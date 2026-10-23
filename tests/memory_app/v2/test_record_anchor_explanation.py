"""Synthetic arithmetic-anchor explanations; no private replay material."""
from copy import deepcopy
from datetime import date, timedelta

import pytest

from core.search_and_recall.evidence_windows import EvidenceWindow
from backend.memory_app.v2.answer_evidence import (
    answer_response_model, build_evidence_catalog, build_partition_messages,
    final_response_model, inspect_referenced_first_draft, reviewed_scope_answer,
)
from backend.memory_app.v2.calendar_review_projection import project_review
from backend.memory_app.v2.policies import ACTIVE
from backend.memory_app.v2.policies.date_grounding import _body, build_date_ref_catalog
from tests.memory_app.v2.test_calendar_review_projection import fixture as prior_fixture


QUESTION = 'When did Tess polish the Copper lens?'


def material(*, expression='two days ago', header=False, local=False):
    anchor = '2042-11-20'
    content = (('Session date: ' + anchor + '\n') if header else '')
    body = ('On ' + anchor + ', ' if local else '') + f'I polished the Copper lens {expression}.'
    content += ('' if local else '[D1:1] Tess: ') + body + '\n'
    chosen = [{'id': 'synthetic-arithmetic-anchor', 'kind': 'source', 'layer': 'L0',
        'title': 'Synthetic record ' + anchor, 'coordinate_space': 'source_content_v1',
        'entry': {'content': content}, 'excerpt': content,
        'windows': (EvidenceWindow(0, len(content), content),)}]
    catalog = build_evidence_catalog(chosen, compose_version='@36')
    refs = build_date_ref_catalog(catalog, chosen, calendar_version=3)
    ref = refs[0]
    result = ref['options'][0]['start']
    text = f'Tess polished the Copper lens on {result} (two days before {anchor}).'
    fact = {'kind': 'date', 'text': text,
            'evidence': [{'unit_id': ref['unit_id'], 'quote': _body(ref['sentence']), 'role': 'support'}],
            'date_refs': [ref['date_ref_id']]}
    first = {'facts': [fact], 'unreviewed': {'scope': 'all_unreferenced', 'reason': 'Synthetic requested event.'}}
    review = {'statements': [{**deepcopy(fact), 'first_fact_uses': [
        {'fact_index': 1, 'action': 'adopt', 'reason': 'Retain the synthetic calculation.'}]}],
        'unused': {'scope': 'all_unreferenced', 'reason': 'Synthetic requested event.'},
        'uncertain': [], 'excluded_facts': []}
    return chosen, catalog, first, review, anchor, result


def set_text(first, review, text):
    first['facts'][0]['text'] = review['statements'][0]['text'] = text


def accept(chosen, catalog, first, review, *, version='@36', callback=None):
    return reviewed_scope_answer(review, catalog, first, chosen=chosen, question=QUESTION,
                                 compose_version=version, _on_projection=callback)


@pytest.mark.parametrize('header', [False, True])
@pytest.mark.parametrize('shape', ['parenthesis', 'equivalence', 'chinese_parenthesis', 'chinese_equivalence'])
def test_complete_same_offset_arithmetic_pair_is_accepted_without_rewriting_model(header, shape):
    chosen, catalog, first, review, anchor, result = material(header=header)
    text = {
        'parenthesis': f'Tess polished the lens on {result} (two days before {anchor}).',
        'equivalence': f'Tess polished the lens two days before {anchor}, i.e. on {result}.',
        'chinese_parenthesis': f'镜片擦亮于{result}（比{anchor}早两天）。',
        'chinese_equivalence': f'镜片擦亮于{anchor}前两天，即{result}。',
    }[shape]
    set_text(first, review, text)
    before = deepcopy((first, review))
    for version in ('@34', '@35'):
        with pytest.raises(ValueError, match='invalid_first_fact_requires_correction'):
            accept(chosen, catalog, first, review, version=version)
    _, _, diagnostics = inspect_referenced_first_draft(first, catalog, chosen=chosen,
        question=QUESTION, compose_version='@36')
    assert diagnostics == []
    reports = []
    value = accept(chosen, catalog, first, review, callback=reports.append)
    assert value.statements[0].kind == 'date' and value.statements[0].text == text
    assert len(value.statements[0].derived_notes) == 1
    assert reports == [{'route': 'unchanged', 'repaired_statements': 0,
                        'fallback_statements': 0, 'codes': []}]
    assert (first, review) == before


@pytest.mark.parametrize('expression,word', [('in two days', 'after'), ('yesterday', 'before')])
def test_existing_forward_and_single_day_source_offsets_keep_direction(expression, word):
    chosen, catalog, first, review, anchor, result = material(expression=expression)
    count = 'one day' if expression == 'yesterday' else 'two days'
    set_text(first, review, f'Tess polished the lens on {result} ({count} {word} {anchor}).')
    assert accept(chosen, catalog, first, review).statements[0].kind == 'date'


@pytest.mark.parametrize('shape', [
    'wrong_count', 'wrong_direction', 'wrong_anchor', 'wrong_result', 'missing_result',
    'event_on_anchor', 'extra_event_on_anchor', 'unrelated_result', 'inserted_event',
    'forward_inserted_event', 'cross_sentence', 'wrong_unit', 'compound_count', 'hyphen_count', 'decimal_count', 'negative_count', 'thousands_count', 'split_negative_count', 'split_operator_count',
    'attached_operator', 'embedded_parenthesis',
])
def test_anchor_occurrence_does_not_grant_other_events_offsets_or_broken_pairing(shape):
    chosen, catalog, first, review, anchor, result = material()
    next_anchor = (date.fromisoformat(anchor) + timedelta(days=1)).isoformat()
    wrong = (date.fromisoformat(result) + timedelta(days=1)).isoformat()
    text = {
        'wrong_count': f'Tess polished on {result} (three days before {anchor}).',
        'wrong_direction': f'Tess polished on {result} (two days after {anchor}).',
        'wrong_anchor': f'Tess polished on {result} (two days before {next_anchor}).',
        'wrong_result': f'Tess polished on {wrong} (two days before {anchor}).',
        'missing_result': f'Tess polished two days before {anchor}.',
        'event_on_anchor': f'Tess polished on {anchor}. Another date was {result}.',
        'extra_event_on_anchor': f'Tess polished on {result} (two days before {anchor}). She travelled on {anchor}.',
        'unrelated_result': f'Tess polished on {result}. Someone else travelled two days before {anchor}.',
        'inserted_event': f'Tess polished on {result} (someone else travelled two days before {anchor}).',
        'forward_inserted_event': f'Tess polished two days before {anchor}, then someone else travelled on {result}.',
        'cross_sentence': f'Tess polished two days before {anchor}. i.e. on {result}.',
        'wrong_unit': f'Tess polished two weeks before {anchor}, i.e. on {result}.',
        'compound_count': f'Tess polished twenty two days before {anchor}, i.e. on {result}.',
        'hyphen_count': f'Tess polished twenty-two days before {anchor}, i.e. on {result}.',
        'decimal_count': f'Tess polished 2.2 days before {anchor}, i.e. on {result}.',
        'negative_count': f'Tess polished -2 days before {anchor}, i.e. on {result}.',
        'thousands_count': f'Tess polished 1,002 days before {anchor}, i.e. on {result}.',
        'split_negative_count': f'Tess polished - 2 days before {anchor}, i.e. on {result}.',
        'split_operator_count': f'Tess polished one / 2 days before {anchor}, i.e. on {result}.',
        'attached_operator': f'Tess polished one/ 2 days before {anchor}, i.e. on {result}.',
        'embedded_parenthesis': f'Tess polished one/( two days before {anchor}, i.e. on {result}.',
    }[shape]
    set_text(first, review, text)
    review['statements'][0]['first_fact_uses'][0]['action'] = 'correct'
    reports = []
    with pytest.raises(ValueError, match='grounded_calendar_provenance_invalid'):
        accept(chosen, catalog, first, review, callback=reports.append)
    assert reports == []


def test_numeric_group_with_whitespace_cannot_be_read_as_its_last_integer_token():
    chosen, catalog, first, review, anchor, result = material(expression='102 days ago')
    set_text(first, review, f'Tess polished 102 days before {anchor}, i.e. on {result}.')
    assert accept(chosen, catalog, first, review).statements[0].kind == 'date'
    set_text(first, review, f'Tess polished 1, 102 days before {anchor}, i.e. on {result}.')
    review['statements'][0]['first_fact_uses'][0]['action'] = 'correct'
    with pytest.raises(ValueError, match='grounded_calendar_provenance_invalid'):
        accept(chosen, catalog, first, review)


@pytest.mark.parametrize('fault', ['missing_ref', 'unknown_ref', 'partial_quote', 'context_only', 'local_event_anchor', 'multiple_refs', 'cross_source'])
def test_actual_reference_and_source_constraints_remain_mandatory(fault):
    chosen, catalog, first, review, anchor, result = material(local=fault == 'local_event_anchor')
    if fault == 'local_event_anchor':
        # An independently explicit source date keeps its old authority.  Only
        # assert that the new record-anchor explanation grants it no privilege.
        from backend.memory_app.v2.answer_evidence import _resolve_reference_statements, ReviewedReferencedStatement
        from backend.memory_app.v2.policies.date_grounding import _is_record_anchor_explanation, calendar_mentions_v1
        statements, bindings = _resolve_reference_statements(
            [ReviewedReferencedStatement.model_validate(review['statements'][0])], catalog, chosen,
            compose_version='@36')
        statement = statements[0]
        record = bindings[id(statement)]
        value = next(item for item in calendar_mentions_v1(statement.text) if item['start'] == anchor)
        assert record[0]['anchor_kind'] == 'local_explicit'
        assert not _is_record_anchor_explanation(statement, value, record, record)
        return
    if fault in {'missing_ref', 'unknown_ref'}:
        for statement in (first['facts'][0], review['statements'][0]):
            statement['date_refs'] = [] if fault == 'missing_ref' else [999]
    elif fault == 'partial_quote':
        for statement in (first['facts'][0], review['statements'][0]):
            statement['evidence'][0]['quote'] = 'I polished the Copper lens'
    elif fault == 'context_only':
        for statement in (first['facts'][0], review['statements'][0]):
            statement['evidence'][0]['role'] = 'context'
    elif fault in {'multiple_refs', 'cross_source'}:
        other = deepcopy(chosen[0])
        other['id'] = 'synthetic-second-record'
        chosen.append(other)
        catalog = build_evidence_catalog(chosen, compose_version='@36')
        refs = build_date_ref_catalog(catalog, chosen, calendar_version=3)
        for statement in (first['facts'][0], review['statements'][0]):
            if fault == 'multiple_refs':
                statement['date_refs'] = [row['date_ref_id'] for row in refs]
                statement['evidence'].append({'unit_id': refs[1]['unit_id'], 'quote': _body(refs[1]['sentence']), 'role': 'support'})
            else:
                statement['date_refs'] = [refs[1]['date_ref_id']]
    review['statements'][0]['first_fact_uses'][0]['action'] = 'correct'
    with pytest.raises(ValueError):
        accept(chosen, catalog, first, review)


def test_36_keeps_the_35_schema_catalog_messages_default_and_prior_projections():
    chosen, catalog, first, review, _, _ = material()
    assert ACTIVE['compose'] == '@34'
    assert catalog == build_evidence_catalog(chosen, compose_version='@35')
    assert answer_response_model('@36') is answer_response_model('@35')
    assert final_response_model('@36', QUESTION) is final_response_model('@35', QUESTION)
    assert build_partition_messages(chosen, QUESTION, compose_version='@36') == build_partition_messages(chosen, QUESTION, compose_version='@35')
    # Review diagnostics intentionally differ, while the model schema/contract stays fixed.
    for bad_value in (False, True):
        old_chosen, old_catalog, old_first, old_review = prior_fixture()
        if bad_value:
            set_text(old_first, old_review, 'Lio tuned it on 2036-07-01.')
        old, old_report = project_review(old_review, old_catalog, old_first, chosen=old_chosen,
                                         question=QUESTION, compose_version='@35')
        new, new_report = project_review(old_review, old_catalog, old_first, chosen=old_chosen,
                                         question=QUESTION, compose_version='@36')
        assert old == new and old_report == new_report
        assert accept(old_chosen, old_catalog, old_first, old_review).statements
