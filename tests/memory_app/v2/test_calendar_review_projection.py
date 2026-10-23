"""Synthetic @35 calendar projections; no private samples or model calls."""
from copy import deepcopy
import json

import pytest

from core.search_and_recall.evidence_windows import EvidenceWindow
from backend.memory_app.v2.answer_evidence import (
    answer_response_model, build_evidence_catalog, build_partition_messages,
    final_response_model, first_fact_partition, inspect_referenced_first_draft,
    render_grounded_answer, reviewed_scope_answer,
)
from backend.memory_app.v2.calendar_review_projection import project_review
from backend.memory_app.v2.policies import ACTIVE, get
from backend.memory_app.v2.policies.date_grounding import _body, build_date_ref_catalog, resolve_date_refs


QUESTION = 'When did Lio tune the Bramble meter?'


def fixture(*, anchor=True, expression='two days ago'):
    header = 'Session date: 2036-04-18\n' if anchor else ''
    body = f'I tuned the Bramble meter {expression}.'
    content = header + '[D1:1] Lio: I drafted notes last week.\n[D1:2] Lio: ' + body + '\n'
    chosen = [{'id': 'synthetic-calendar-source', 'kind': 'source', 'layer': 'L0',
        'title': 'Synthetic dialogue', 'coordinate_space': 'source_content_v1',
        'entry': {'content': content}, 'excerpt': content,
        'windows': (EvidenceWindow(0, len(content), content),)}]
    catalog = build_evidence_catalog(chosen, compose_version='@35')
    refs = build_date_ref_catalog(catalog, chosen, calendar_version=3)
    ref = next((row for row in refs if body in row['sentence']), None)
    unit = next(row for row in catalog['units'] if body in row['text'])
    day = ref['options'][0]['start'] if ref else '2036-04-16'
    first = {'facts': [{'kind': 'fact', 'text': f'Lio tuned the Bramble meter on {day}.',
        'evidence': [{'unit_id': unit['id'], 'quote': _body(ref['sentence']) if ref else body,
                      'role': 'support'}], 'date_refs': [ref['date_ref_id']] if ref else []}],
        'unreviewed': {'scope': 'all_unreferenced', 'reason': 'Only the requested synthetic event.'}}
    final = {'statements': [{**deepcopy(first['facts'][0]), 'first_fact_uses': [
        {'fact_index': 1, 'action': 'adopt', 'reason': 'Retain the requested event.'}]}],
        'unused': {'scope': 'all_unreferenced', 'reason': 'Only the requested synthetic event.'},
        'uncertain': [], 'excluded_facts': []}
    return chosen, catalog, first, final


def checked(final, catalog, first, chosen, *, version='@35', callback=None):
    return reviewed_scope_answer(final, catalog, first, chosen=chosen, question=QUESTION,
                                 compose_version=version, _on_projection=callback)


def render(value, catalog, first, chosen):
    return render_grounded_answer(value, catalog, first_fact_partition(first, catalog, compose_version='@35'),
        max_output_tokens=3000, chosen=chosen,
        **get('compose', version='@35')(None, operation='grounded_render', question=QUESTION))


def test_exact_calendar_classification_repair_preserves_originals_and_all_core_evidence():
    chosen, catalog, first, final = fixture()
    before = deepcopy((chosen, catalog, first, final))
    assert first['facts'][0]['date_refs'] == [2]
    with pytest.raises(ValueError, match='invalid_first_fact_requires_correction'):
        checked(final, catalog, first, chosen, version='@34')
    only_action = deepcopy(final)
    only_action['statements'][0]['first_fact_uses'][0]['action'] = 'correct'
    with pytest.raises(ValueError, match='grounded_calendar_provenance_invalid'):
        checked(only_action, catalog, first, chosen, version='@34')
    projected, report = project_review(final, catalog, first, chosen=chosen, question=QUESTION)
    assert report == {'route': 'classification_repair', 'repaired_statements': 1,
                      'fallback_statements': 0, 'codes': ['calendar_classification_corrected']}
    assert projected['statements'][0]['kind'] == 'date'
    assert projected['statements'][0]['first_fact_uses'][0]['action'] == 'correct'
    for key in ('text', 'evidence', 'date_refs'):
        assert projected['statements'][0][key] == final['statements'][0][key]
    value = checked(final, catalog, first, chosen)
    assert value.statements[0].kind == 'date' and len(value.statements[0].derived_notes) == 1
    assert (chosen, catalog, first, final) == before
    checked(projected, catalog, first, chosen, version='@34')


def test_callback_runs_once_only_after_success_and_report_has_no_material():
    chosen, catalog, first, final = fixture()
    reports = []
    checked(final, catalog, first, chosen, callback=reports.append)
    assert len(reports) == 1 and reports[0]['route'] == 'classification_repair'
    text = json.dumps(reports)
    assert all(value not in text for value in (QUESTION, 'Lio', 'Bramble', '2036', first['facts'][0]['text']))
    final['statements'][0]['date_refs'] = [999]
    reports.clear()
    with pytest.raises(ValueError):
        checked(final, catalog, first, chosen, callback=reports.append)
    assert reports == []


def test_candidate_registry_has_same_schema_messages_calendar_and_keeps_default():
    chosen, catalog, first, _ = fixture()
    assert ACTIVE['compose'] == '@34'
    assert catalog == build_evidence_catalog(chosen, compose_version='@34')
    assert catalog['calendar_version'] == 3
    assert answer_response_model('@35').model_json_schema() == answer_response_model('@34').model_json_schema()
    assert final_response_model('@35', QUESTION).model_json_schema() == final_response_model('@34', QUESTION).model_json_schema()
    for kwargs in ({}, {'first_output': first}):
        assert build_partition_messages(chosen, QUESTION, compose_version='@35', **kwargs) == build_partition_messages(
            chosen, QUESTION, compose_version='@34', **kwargs)
    assert get('compose', version='@35')(None, operation='answer_contract') == get(
        'compose', version='@34')(None, operation='answer_contract')


def test_model_already_repaired_review_is_unchanged_and_old_callback_is_not_emitted():
    chosen, catalog, first, final = fixture()
    final['statements'][0]['kind'] = 'date'
    final['statements'][0]['first_fact_uses'][0]['action'] = 'correct'
    before = deepcopy(final)
    projected, report = project_review(final, catalog, first, chosen=chosen, question=QUESTION)
    assert projected == before and report['route'] == 'unchanged'
    reports = []
    checked(final, catalog, first, chosen, version='@34', callback=reports.append)
    assert reports == []
    checked(final, catalog, first, chosen, callback=reports.append)
    assert reports == [report]


@pytest.mark.parametrize('uncertainty', ['wrong_value', 'missing_anchor', 'ambiguous_week'])
def test_uncertain_conversion_quotes_original_with_speaker_and_fixed_limitation(uncertainty):
    chosen, catalog, first, final = fixture(anchor=uncertainty != 'missing_anchor',
        expression='last week' if uncertainty == 'ambiguous_week' else 'two days ago')
    unsupported = '2036-07-01'
    if uncertainty != 'missing_anchor':
        first['facts'][0]['text'] = final['statements'][0]['text'] = f'Lio tuned it on {unsupported}.'
    else:
        unsupported = '2036-04-16'
    before = deepcopy((first, final))
    projected, report = project_review(final, catalog, first, chosen=chosen, question=QUESTION)
    assert report['route'] == 'evidence_fallback' and report['fallback_statements'] == 1
    assert report['repaired_statements'] == 0
    assert projected['statements'][0]['kind'] == 'fact'
    assert projected['statements'][0]['date_refs'] == []
    assert projected['statements'][0]['evidence'] == final['statements'][0]['evidence']
    text = projected['statements'][0]['text']
    assert '[D1:2] Lio:' in text and final['statements'][0]['evidence'][0]['quote'] in text
    assert unsupported not in text
    assert projected['statements'][1]['kind'] == 'limitation'
    assert projected['statements'][1]['text'] == '本轮换算未验证。'
    value = checked(final, catalog, first, chosen)
    public = render(value, catalog, first, chosen)
    assert unsupported not in public.answer and 'Lio:' in public.answer
    assert '本轮换算未验证' in public.answer and 'No supported answer' not in public.answer
    assert public.citations == [1]
    assert (first, final) == before
    checked(projected, catalog, first, chosen, version='@34')


def test_valid_other_fact_is_preserved_alongside_a_classification_repair():
    chosen, catalog, first, final = fixture()
    unit = next(row for row in catalog['units'] if 'I drafted notes last week.' in row['text'])
    extra = {'kind': 'fact', 'text': 'Lio said: I drafted notes last week.',
        'evidence': [{'unit_id': unit['id'], 'quote': 'I drafted notes last week.', 'role': 'support'}], 'date_refs': []}
    first['facts'].append(extra)
    final['statements'].append({**deepcopy(extra), 'first_fact_uses': [
        {'fact_index': 2, 'action': 'adopt', 'reason': 'Retain the separate source quotation.'}]})
    value = checked(final, catalog, first, chosen)
    assert [row.kind for row in value.statements] == ['date', 'fact']
    assert value.statements[1].text == extra['text']


@pytest.mark.parametrize('fault', [
    'first_unknown_ref', 'review_unknown_ref', 'partial_sentence', 'forged_first_quote',
    'forged_review_quote', 'unknown_unit', 'position', 'duplicate_position', 'missing_position',
    'empty_reason', 'no_support', 'catalog_changed', 'source_changed', 'inference', 'changed_core',
])
def test_hard_provenance_positions_or_wider_semantics_are_not_hidden_by_fallback(fault):
    chosen, catalog, first, final = fixture()
    if fault == 'first_unknown_ref':
        first['facts'][0]['date_refs'] = [999]
    elif fault == 'review_unknown_ref':
        final['statements'][0]['date_refs'] = [999]
    elif fault == 'partial_sentence':
        for row in (first['facts'][0], final['statements'][0]):
            row['evidence'][0]['quote'] = 'I tuned the Bramble meter'
    elif fault == 'forged_first_quote':
        first['facts'][0]['evidence'][0]['quote'] = 'Fabricated source claim.'
    elif fault == 'forged_review_quote':
        final['statements'][0]['evidence'][0]['quote'] = 'Fabricated source claim.'
    elif fault == 'unknown_unit':
        final['statements'][0]['evidence'][0]['unit_id'] = 999
    elif fault == 'position':
        final['statements'][0]['first_fact_uses'][0]['fact_index'] = 999
    elif fault == 'duplicate_position':
        final['statements'][0]['first_fact_uses'] *= 2
    elif fault == 'missing_position':
        final['statements'][0]['first_fact_uses'] = []
    elif fault == 'empty_reason':
        final['statements'][0]['first_fact_uses'][0]['reason'] = ' '
    elif fault == 'no_support':
        for row in (first['facts'][0], final['statements'][0]):
            row['evidence'][0]['role'] = 'context'
    elif fault == 'catalog_changed':
        catalog['units'][-1]['text'] += 'Changed.'
    elif fault == 'source_changed':
        chosen[0]['entry']['content'] += 'Changed.'
    elif fault == 'inference':
        for row in (first['facts'][0], final['statements'][0]):
            row['kind'] = 'inference'
    else:
        final['statements'][0]['text'] += ' A different event.'
    reports = []
    with pytest.raises(ValueError):
        checked(final, catalog, first, chosen, callback=reports.append)
    assert reports == []


def test_date_grounding_public_35_alias_retains_calendar3_notes_and_fact_non_authority():
    from backend.memory_app.v2.answer_evidence import ReferencedStatement
    chosen, catalog, first, _ = fixture()
    refs = build_date_ref_catalog(catalog, chosen, calendar_version=3)
    statement = ReferencedStatement.model_validate(first['facts'][0])
    assert resolve_date_refs(statement, catalog, chosen, refs, compose_version='@35') == ([], [])
    records, notes = resolve_date_refs(statement.model_copy(update={'kind': 'date'}), catalog, chosen,
                                      refs, compose_version='@35')
    assert len(records) == len(notes) == 1
    _, _, old = inspect_referenced_first_draft(first, catalog, chosen=chosen, question=QUESTION, compose_version='@34')
    _, _, new = inspect_referenced_first_draft(first, catalog, chosen=chosen, question=QUESTION, compose_version='@35')
    assert old == new


def test_nested_quotation_is_not_rebound_as_the_record_speakers_first_person():
    quote = 'I folded the Glint map yesterday.'
    prefix = '[D4:2] Emil:'
    content = prefix + ' Rowan said, "' + quote + '"\n'
    chosen = [{'id': 'synthetic-nested-quotation', 'kind': 'source', 'layer': 'L0',
        'title': 'Synthetic nested dialogue', 'coordinate_space': 'source_content_v1',
        'entry': {'content': content}, 'excerpt': content,
        'windows': (EvidenceWindow(0, len(content), content),)}]
    catalog = build_evidence_catalog(chosen, compose_version='@35')
    first = {'facts': [{'kind': 'fact', 'text': 'Rowan folded the Glint map on 2044-05-06.',
        'evidence': [{'unit_id': catalog['units'][0]['id'], 'quote': quote, 'role': 'support'}],
        'date_refs': []}], 'unreviewed': {'scope': 'all_unreferenced', 'reason': 'Synthetic nested quote.'}}
    final = {'statements': [{**deepcopy(first['facts'][0]), 'first_fact_uses': [
        {'fact_index': 1, 'action': 'adopt', 'reason': 'Retain the synthetic event.'}]}],
        'unused': {'scope': 'all_unreferenced', 'reason': 'Synthetic nested quote.'},
        'uncertain': [], 'excluded_facts': []}
    before = deepcopy((first, final))
    projected, report = project_review(final, catalog, first, chosen=chosen,
                                      question='When did Rowan fold the Glint map?')
    assert report['route'] == 'evidence_fallback' and report['fallback_statements'] == 1
    text = projected['statements'][0]['text']
    assert '记录层标签（不判定片段内人称）：' + prefix in text
    assert '原文片段：' + quote in text
    assert prefix + ' ' + quote not in text and 'Emil: I folded' not in text
    assert projected['statements'][0]['evidence'] == first['facts'][0]['evidence']
    assert projected['statements'][1]['text'] == '本轮换算未验证。'
    value = reviewed_scope_answer(final, catalog, first, chosen=chosen,
        question='When did Rowan fold the Glint map?', compose_version='@35')
    public = render(value, catalog, first, chosen)
    assert prefix + ' ' + quote not in public.answer and 'Emil: I folded' not in public.answer
    assert '本轮换算未验证' in public.answer and '2044-05-06' not in public.answer
    assert (first, final) == before
