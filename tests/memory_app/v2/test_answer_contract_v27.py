"""日期引用、逐位置纠正与有限输出的真实纯函数控制。"""
from copy import deepcopy

import pytest

from core.search_and_recall.evidence_windows import EvidenceWindow
from backend.memory_app.v2.answer_evidence import (
    answer_response_model, build_evidence_catalog, build_partition_messages,
    final_response_model, first_fact_empty, first_facts_output_minimum,
    inspect_referenced_first_draft, reviewed_scope_answer, validate_first_facts,
)
from backend.memory_app.v2.policies import ACTIVE, get
from backend.memory_app.v2.policies.date_grounding import build_date_ref_catalog
from backend.memory_app.answer_proof import review_input_tokens


@pytest.fixture
def material():
    content = 'Session date: 2023-08-23\n[D1:1] Maya: I painted this self-portrait last week.\n'
    chosen = [{'id': 'session-one', 'kind': 'source', 'title': 'Session 2023-08-23', 'excerpt': content,
        'coordinate_space': 'source_content_v1', 'entry': {'content': content},
        'windows': (EvidenceWindow(0, len(content), content),)}]
    catalog = build_evidence_catalog(chosen)
    refs = build_date_ref_catalog(catalog, chosen)
    assert len(refs) == 1
    ref = refs[0]
    evidence = [{'unit_id': ref['unit_id'], 'quote': 'I painted this self-portrait last week.', 'role': 'support'}]
    fact = {'kind': 'fact', 'text': 'Maya said she painted her self-portrait last week.',
        'evidence': evidence, 'date_refs': [ref['date_ref_id']]}
    first = {'facts': [fact], 'unreviewed': {'scope': 'all_unreferenced', 'reason': 'Only the requested painting event.'}}
    return chosen, catalog, first


def final_for(first, *, action='correct'):
    statement = deepcopy(first['facts'][0])
    statement['date_refs'] = []
    statement['first_fact_uses'] = [{'fact_index': 1, 'action': action,
        'reason': 'Removed calculation refs from the unchanged relative quotation fact.'}]
    return {'statements': [statement], 'unused': {'scope': 'all_unreferenced', 'reason': 'Only requested evidence.'},
        'uncertain': [], 'excluded_facts': []}


def test_nondate_ref_is_explicit_diagnostic_and_actual_correction_passes(material):
    chosen, catalog, first = material
    raw = deepcopy(first)
    _, _, diagnostics = inspect_referenced_first_draft(first, catalog, chosen=chosen,
        question='When did Maya paint this?', compose_version='@27')
    assert diagnostics == [{'fact_index': 1, 'codes': ['grounded_date_ref_kind_invalid']}]
    assert first == raw
    result = reviewed_scope_answer(final_for(first), catalog, first, chosen=chosen,
        question='When did Maya paint this?', compose_version='@27')
    assert result.statements[0].kind == 'fact'
    assert result.statements[0].derived_notes == []
    with pytest.raises(ValueError, match='invalid_first_fact_requires_correction'):
        reviewed_scope_answer(final_for(first, action='adopt'), catalog, first, chosen=chosen,
            question='When did Maya paint this?', compose_version='@27')


def test_natural_record_anchor_with_complete_week_passes_without_legacy_recheck(material):
    chosen, catalog, first = material
    fact = first['facts'][0]
    fact['kind'] = 'date'
    fact['text'] = ('Maya painted it in the week before 23 August 2023: '
        '2023-08-14 to 2023-08-20, or 2023-08-16 to 2023-08-22 under the rolling-week convention.')
    _, _, diagnostics = inspect_referenced_first_draft(first, catalog, chosen=chosen,
        question='When did Maya paint this?', compose_version='@27')
    assert diagnostics == []
    final = final_for(first, action='adopt')
    final['statements'][0]['date_refs'] = fact['date_refs']
    result = reviewed_scope_answer(final, catalog, first, chosen=chosen,
        question='When did Maya paint this?', compose_version='@27')
    assert result.statements[0].kind == 'date'
    with pytest.raises(ValueError, match='invalid_first_fact_requires_correction'):
        reviewed_scope_answer(final, catalog, first, chosen=chosen,
            question='When did Maya paint this?', compose_version='@26')


def test_new_private_schema_and_two_call_budget_are_frozen_separately():
    assert answer_response_model('@27').__name__ == 'FocusedReferencedFactsOutput'
    assert final_response_model('@27', 'Question?').__name__ == 'FocusedReviewedScopeOutput'
    assert answer_response_model('@26').__name__ == 'FirstReferencedFactsOutput'
    assert {name: ACTIVE[name] for name in ('compose', 'ladder', 'retrieve', 'rerank')} == {
        'compose': '@30', 'ladder': '@15', 'retrieve': '@5', 'rerank': '@2'}
    assert get('compose', version='@27')(None, operation='answer_contract')['nested_model_handle_budget'] == 4


def test_final_diagnostics_describe_actual_field_repairs(material):
    chosen, _, first = material
    messages = build_partition_messages(chosen, 'When did Maya paint this?', first_output=first, compose_version='@27')
    assert 'grounded_date_ref_kind_invalid' in messages[-1]['content']
    assert 'action=correct' in messages[0]['content']
    assert 'date_refs: []' in messages[0]['content']
    assert 'A reason saying corrected does not change the actual fields' in messages[0]['content']


def test_reason_alone_cannot_correct_nondate_refs(material):
    chosen, catalog, first = material
    final = final_for(first)
    final['statements'][0]['date_refs'] = [1]
    final['statements'][0]['first_fact_uses'][0]['reason'] = 'Corrected kind from fact to date.'
    with pytest.raises(ValueError, match='grounded_date_ref_kind_invalid'):
        reviewed_scope_answer(final, catalog, first, chosen=chosen,
            question='When did Maya paint this?', compose_version='@27')


@pytest.mark.parametrize('action', ['adopt', 'merge'])
def test_diagnosed_positions_require_actual_correction_or_exclusion(material, action):
    chosen, catalog, first = material
    with pytest.raises(ValueError, match='invalid_first_fact_requires_correction'):
        reviewed_scope_answer(final_for(first, action=action), catalog, first, chosen=chosen,
            question='When did Maya paint this?', compose_version='@27')
    final = final_for(first)
    final['statements'][0]['first_fact_uses'] = []
    final['excluded_facts'] = [{'fact_indices': [1], 'reason': 'Bad field form; independently restated without calculation.'}]
    assert reviewed_scope_answer(final, catalog, first, chosen=chosen,
        question='When did Maya paint this?', compose_version='@27').statements[0].derived_notes == []


@pytest.mark.parametrize('bad_text', [
    'Maya painted it on 2023-08-23.',
    'Maya painted it on 2023-08-14.',
    'Maya painted it in 2023-08-14 to 2023-08-22.',
    'Maya painted it in the week before 24 August 2023: 2023-08-14 to 2023-08-20.',
    'Maya painted it in the week before 23 August 2023.',
])
def test_anchor_is_not_event_day_and_incomplete_ranges_are_not_authorized(material, bad_text):
    chosen, catalog, first = material
    first['facts'][0].update(kind='date', text=bad_text)
    _, _, diagnostics = inspect_referenced_first_draft(first, catalog, chosen=chosen,
        question='When did Maya paint this?', compose_version='@27')
    assert any('grounded_calendar_provenance_invalid' in item['codes'] for item in diagnostics)
    final = final_for(first)
    final['statements'][0]['date_refs'] = [1]
    with pytest.raises(ValueError, match='grounded_calendar_provenance_invalid'):
        reviewed_scope_answer(final, catalog, first, chosen=chosen,
            question='When did Maya paint this?', compose_version='@27')


def test_unknown_date_ref_and_fabricated_quote_still_fail_hard(material):
    chosen, catalog, first = material
    first['facts'][0]['date_refs'] = [999]
    with pytest.raises(ValueError, match='grounded_date_ref_invalid'):
        validate_first_facts(first, catalog, chosen=chosen, question='When?', compose_version='@27')
    first['facts'][0]['date_refs'] = []
    first['facts'][0]['evidence'][0]['quote'] = 'I painted it on 2023-08-14.'
    with pytest.raises(ValueError, match='grounded_evidence_invalid'):
        validate_first_facts(first, catalog, chosen=chosen, question='When?', compose_version='@27')


def test_complete_duplicate_is_diagnosed_without_mutating_first_then_excluded(material):
    chosen, catalog, first = material
    first['facts'][0]['date_refs'] = []
    first['facts'].append(deepcopy(first['facts'][0]))
    original = deepcopy(first)
    _, _, diagnostics = inspect_referenced_first_draft(first, catalog, chosen=chosen,
        question='What did Maya paint?', compose_version='@27')
    assert diagnostics == [{'fact_index': 2, 'codes': ['grounded_statement_duplicate']}]
    assert first == original
    final = final_for(first, action='adopt')
    final['excluded_facts'] = [{'fact_indices': [2], 'reason': 'Exact duplicate of the retained first proposition.'}]
    assert len(reviewed_scope_answer(final, catalog, first, chosen=chosen,
        question='What did Maya paint?', compose_version='@27').statements) == 1
    final['statements'].append(deepcopy(final['statements'][0]))
    with pytest.raises(ValueError, match='grounded_statement_duplicate'):
        reviewed_scope_answer(final, catalog, first, chosen=chosen,
            question='What did Maya paint?', compose_version='@27')


def test_one_quote_can_support_distinct_requested_meanings(material):
    chosen, catalog, first = material
    first['facts'][0]['date_refs'] = []
    first['facts'].append(deepcopy(first['facts'][0]))
    first['facts'][1]['text'] = 'Maya described the painting as a self-portrait.'
    _, _, diagnostics = inspect_referenced_first_draft(first, catalog, chosen=chosen,
        question='What did Maya say about the painting?', compose_version='@27')
    assert diagnostics == []


def test_sparse_empty_first_is_used_for_actual_budget_admission(material):
    chosen, catalog, _ = material
    empty = first_fact_empty('@27', catalog)
    assert set(empty) == {'facts', 'unreviewed'}
    assert empty['facts'] == []
    validate_first_facts(empty, catalog, chosen=chosen, question='Question?', compose_version='@27')
    assert first_facts_output_minimum(catalog, compose_version='@27') == first_facts_output_minimum(catalog, compose_version='@26')
    first = answer_response_model('@27')
    final = final_response_model('@27', 'Question?')
    assert answer_response_model(None, frozen_schema=first.model_json_schema()) is first
    assert answer_response_model(None, frozen_schema=final.model_json_schema()) is final


def test_short_material_fits_original_unknown_remote_input_budget(material):
    chosen, _, first = material
    messages = build_partition_messages(chosen, 'When did Maya paint this?', compose_version='@27')
    reviewed = build_partition_messages(chosen, 'When did Maya paint this?', first_output=first, compose_version='@27')
    assert 'grounded_date_ref_kind_invalid' in reviewed[-1]['content']
    assert review_input_tokens(messages, answer_response_model('@27')) <= 4300
    assert review_input_tokens(reviewed, final_response_model('@27', 'When?')) <= 4300
