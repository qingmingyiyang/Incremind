"""合法附带日期引用只容错字段，不授权普通事实进行日期计算。"""
from copy import deepcopy

import pytest

from backend.memory_app.v2.answer_evidence import (
    answer_response_model, build_evidence_catalog,
    build_partition_messages, final_response_model, inspect_referenced_first_draft,
    reviewed_scope_answer, validate_first_facts,
)
from backend.memory_app.v2.policies import ACTIVE, get
from backend.memory_app.v2.policies.date_grounding import build_date_ref_catalog, resolve_date_refs
from backend.memory_app.answer_proof import review_input_tokens
from core.search_and_recall.evidence_windows import EvidenceWindow
from tests.memory_app.v2.test_answer_contract_v27 import material as _material_fixture

material = _material_fixture


def final_for(first, *, action='adopt'):
    fact = deepcopy(first['facts'][0])
    fact['first_fact_uses'] = [{'fact_index': 1, 'action': action, 'reason': 'Retained the source-grounded relative claim.'}]
    return {'statements': [fact], 'unused': {'scope': 'all_unreferenced', 'reason': 'Only the requested fact.'},
        'uncertain': [], 'excluded_facts': []}


@pytest.mark.parametrize('kind', ['fact', 'inference'])
def test_valid_nondate_refs_have_same_authority_as_empty_refs_and_raw_is_preserved(material, kind):
    chosen, catalog, first = material
    first['facts'][0]['kind'] = kind
    original = deepcopy(first)
    raw, normalized, diagnostics = inspect_referenced_first_draft(first, catalog, chosen=chosen,
        question='What did Maya paint?', compose_version='@28')
    assert diagnostics == []
    assert raw.model_dump() == original
    assert normalized.facts[0].derived_notes == []
    refs = build_date_ref_catalog(catalog, chosen)
    assert resolve_date_refs(raw.facts[0], catalog, chosen, refs, compose_version='@28') == ([], [])
    empty = raw.facts[0].model_copy(update={'date_refs': []})
    assert resolve_date_refs(empty, catalog, chosen, refs, compose_version='@28') == ([], [])
    final = final_for(first)
    original_final = deepcopy(final)
    answer = reviewed_scope_answer(final, catalog, first, chosen=chosen,
        question='What did Maya paint?', compose_version='@28')
    assert answer.statements[0].derived_notes == []
    assert answer.statements[0].kind == kind
    assert first == original and final == original_final
    _, _, old_diagnostics = inspect_referenced_first_draft(first, catalog, chosen=chosen,
        question='What did Maya paint?', compose_version='@27')
    assert old_diagnostics == [{'fact_index': 1, 'codes': ['grounded_date_ref_kind_invalid']}]
    with pytest.raises(ValueError, match='invalid_first_fact_requires_correction'):
        reviewed_scope_answer(final, catalog, first, chosen=chosen, question='What did Maya paint?', compose_version='@27')


@pytest.mark.parametrize('kind', ['fact', 'inference'])
@pytest.mark.parametrize('text', ['Maya painted it on 2023-08-14.', 'Maya painted it in 2023-08-14 to 2023-08-20.'])
def test_nondate_computed_calendar_still_requires_real_date_kind(material, kind, text):
    chosen, catalog, first = material
    first['facts'][0].update(kind=kind, text=text)
    _, _, diagnostics = inspect_referenced_first_draft(first, catalog, chosen=chosen,
        question='When did Maya paint it?', compose_version='@28')
    assert diagnostics == [{'fact_index': 1, 'codes': ['grounded_calendar_provenance_invalid']}]
    with pytest.raises(ValueError, match='grounded_calendar_provenance_invalid'):
        reviewed_scope_answer(final_for(first, action='correct'), catalog, first, chosen=chosen,
            question='When did Maya paint it?', compose_version='@28')


@pytest.mark.parametrize('bad_refs', [[999], [1, 1]])
def test_bad_refs_still_fail_before_nondate_normalization(material, bad_refs):
    chosen, catalog, first = material
    first['facts'][0]['date_refs'] = bad_refs
    with pytest.raises(ValueError, match='grounded_date_ref_invalid'):
        validate_first_facts(first, catalog, chosen=chosen, question='What?', compose_version='@28')
    final = final_for(first)
    first['facts'][0]['date_refs'] = []
    with pytest.raises(ValueError, match='grounded_date_ref_invalid'):
        reviewed_scope_answer(final, catalog, first, chosen=chosen, question='What?', compose_version='@28')


def test_ref_requires_matching_real_unit_and_quote_even_for_nondate(material):
    chosen, _, first = material
    content = '[D2:1] Maya: I enjoy painting.'
    chosen.append({'id': 'ordinary', 'kind': 'source', 'title': 'Painting', 'excerpt': content,
        'coordinate_space': 'source_content_v1', 'entry': {'content': content},
        'windows': (EvidenceWindow(0, len(content), content),)})
    catalog = build_evidence_catalog(chosen)
    ordinary = next(unit for unit in catalog['units'] if unit['source_number'] == 2)
    first['facts'][0]['evidence'] = [{'unit_id': ordinary['id'], 'quote': 'I enjoy painting.', 'role': 'support'}]
    with pytest.raises(ValueError, match='grounded_date_ref_invalid'):
        validate_first_facts(first, catalog, chosen=chosen, question='What?', compose_version='@28')


def test_nondate_refs_cannot_authorize_a_later_calendar_limitation(material):
    chosen, catalog, first = material
    limitation = deepcopy(first['facts'][0])
    limitation.update(kind='limitation', text='The evidence only establishes 2023-08-14 to 2023-08-20.')
    limitation['evidence'][0]['role'] = 'context'
    first['facts'].append(limitation)
    _, _, diagnostics = inspect_referenced_first_draft(first, catalog, chosen=chosen,
        question='What did Maya paint?', compose_version='@28')
    assert diagnostics == [{'fact_index': 2, 'codes': ['grounded_calendar_provenance_invalid']}]
    final = final_for(first)
    final['statements'].append({**deepcopy(limitation), 'first_fact_uses': [
        {'fact_index': 2, 'action': 'correct', 'reason': 'Reviewed the precision.'}]})
    with pytest.raises(ValueError, match='grounded_calendar_provenance_invalid'):
        reviewed_scope_answer(final, catalog, first, chosen=chosen, question='What did Maya paint?', compose_version='@28')


def test_complete_duplicate_cannot_escape_by_changing_valid_nondate_refs():
    content = 'Session date: 2023-08-23\n[D1:1] Maya: I painted last week. I drew yesterday.\n'
    chosen = [{'id': 'two-dates', 'kind': 'source', 'title': 'Session 2023-08-23', 'excerpt': content,
        'coordinate_space': 'source_content_v1', 'entry': {'content': content},
        'windows': (EvidenceWindow(0, len(content), content),)}]
    catalog = build_evidence_catalog(chosen)
    refs = build_date_ref_catalog(catalog, chosen)
    assert len(refs) == 2 and refs[0]['unit_id'] == refs[1]['unit_id']
    fact = {'kind': 'fact', 'text': 'Maya painted and drew.', 'evidence': [{'unit_id': refs[0]['unit_id'],
        'quote': 'I painted last week. I drew yesterday.', 'role': 'support'}], 'date_refs': [1]}
    first = {'facts': [fact, {**deepcopy(fact), 'date_refs': [2]}],
        'unreviewed': {'scope': 'all_unreferenced', 'reason': 'Only requested activities.'}}
    _, _, diagnostics = inspect_referenced_first_draft(first, catalog, chosen=chosen,
        question='What did Maya do?', compose_version='@28')
    assert diagnostics == [{'fact_index': 2, 'codes': ['grounded_statement_duplicate']}]
    final = final_for(first)
    final['statements'].append({**deepcopy(fact), 'date_refs': [2], 'first_fact_uses': [
        {'fact_index': 2, 'action': 'correct', 'reason': 'Repeated claim with another valid ref.'}]})
    with pytest.raises(ValueError, match='grounded_statement_duplicate'):
        reviewed_scope_answer(final, catalog, first, chosen=chosen, question='What did Maya do?', compose_version='@28')


@pytest.mark.parametrize('version', ['@27', '@28'])
def test_date_and_precision_limitation_keep_strict_existing_authority(material, version):
    chosen, catalog, first = material
    first['facts'][0].update(kind='date', text='Maya painted it in the week before 23 August 2023: 2023-08-14 to 2023-08-20.')
    limitation = deepcopy(first['facts'][0])
    limitation.update(kind='limitation', text='2023-08-14 to 2023-08-20 is a week range, not an exact event day.')
    limitation['evidence'][0]['role'] = 'context'
    first['facts'].append(limitation)
    _, _, diagnostics = inspect_referenced_first_draft(first, catalog, chosen=chosen,
        question='When did Maya paint?', compose_version=version)
    assert diagnostics == []
    final = final_for(first)
    final['statements'].append({**deepcopy(limitation), 'first_fact_uses': [
        {'fact_index': 2, 'action': 'adopt', 'reason': 'Retained exact precision limit.'}]})
    assert len(reviewed_scope_answer(final, catalog, first, chosen=chosen,
        question='When did Maya paint?', compose_version=version).statements) == 2


def test_v28_reuses_schema_keeps_default_and_four_handle_budget(material):
    chosen, _, first = material
    assert answer_response_model('@28') is answer_response_model('@27')
    assert final_response_model('@28', 'What?') is final_response_model('@27', 'What?')
    assert {name: ACTIVE[name] for name in ('compose', 'ladder', 'retrieve', 'rerank')} == {
        'compose': '@30', 'ladder': '@15', 'retrieve': '@5', 'rerank': '@2'}
    assert get('compose', version='@28')(None, operation='answer_contract')['nested_model_handle_budget'] == 4
    messages = build_partition_messages(chosen, 'What did Maya paint?', first_output=first, compose_version='@28')
    assert 'first_fact_diagnostics": []' in messages[-1]['content']
    assert 'attached refs grant no calculation authority' in messages[0]['content']
    assert 'fact/inference require date_refs: []' not in messages[0]['content']
    assert 'Before unknown/denial, check the full catalog' in messages[0]['content']
    assert "Do not assign another speaker's my/we" in messages[0]['content']
    assert 'compress wording, not item count or representative examples' in messages[0]['content']
    assert 'Source image captions are evidence' in messages[0]['content']
    assert 'Use source-time context' in messages[0]['content']
    assert review_input_tokens(messages, final_response_model('@28', 'What?')) <= 4500
