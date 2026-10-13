"""机械复核队列补全可见位置，不把未引用或问句冒充语义结论。"""
from copy import deepcopy
import json

import pytest

from backend.memory_app.v2.answer_evidence import (
    build_evidence_catalog, build_partition_messages, final_response_model,
    inspect_referenced_first_draft, reviewed_scope_answer,
)
from backend.memory_app.v2.policies import ACTIVE, get
from backend.memory_app.answer_proof import review_input_tokens
from core.search_and_recall.evidence_windows import EvidenceWindow
from tests.memory_app.v2.test_answer_contract_v27 import material as _material_fixture
from tests.memory_app.v2.test_answer_contract_v28 import final_for

material = _material_fixture


def chosen_for(content):
    return [{'id': 'source-one', 'kind': 'source', 'title': 'Notes', 'excerpt': content,
        'coordinate_space': 'source_content_v1', 'entry': {'content': content},
        'windows': (EvidenceWindow(0, len(content), content),)}]


def payload(messages):
    return json.loads(messages[-1]['content'].split('资料：\n', 1)[1].rsplit('\n\n问题：', 1)[0])


def queue(catalog, first=None, diagnostics=(), date_refs=()):
    return get('compose', version='@31')(None, operation='review_queue', catalog=catalog,
        first_output=first, diagnostics=diagnostics, date_refs=date_refs)


def test_unreferenced_independent_planned_activity_is_explicitly_queued():
    content = '[D1:1] Nora: I volunteer at the community center.\n[D1:2] Nora: We are organizing a science fair next month.\n'
    catalog = build_evidence_catalog(chosen_for(content))
    first = {'facts': [{'kind': 'fact', 'text': 'Nora volunteers at a community center.',
        'evidence': [{'unit_id': 1, 'quote': 'I volunteer at the community center.', 'role': 'support'}], 'date_refs': []}],
        'unreviewed': {'scope': 'all_unreferenced', 'reason': 'Only volunteering.'}}
    before = deepcopy((catalog, first))
    review = queue(catalog, first)
    assert review['unreferenced_by_source'] == [{'source_number': 1, 'unit_ids': [2]}]
    assert (catalog, first) == before
    assert 'science' not in json.dumps(review)


def test_caption_components_and_question_clauses_have_exact_unit_local_spans():
    content = '[D1:1] Nora: A gift. [Image caption: a ring with a star and a leaf]\n[D1:2] Nora: The star means hope, right? Green suggests renewal?\n'
    catalog = build_evidence_catalog(chosen_for(content))
    review = queue(catalog)
    details = {row['unit_id']: row for row in review['detail_spans']}
    caption = details[1]['caption_spans'][0]
    assert catalog['units'][0]['text'][slice(*caption)] == '[Image caption: a ring with a star and a leaf]'
    questions = [catalog['units'][1]['text'][slice(*span)] for span in details[2]['question_spans']]
    assert questions == ['The star means hope, right?', 'Green suggests renewal?']
    assert review['unreferenced_by_source'] == [{'source_number': 1, 'unit_ids': [1, 2]}]


def test_partial_quotes_keep_unquoted_independent_subitems_and_ignore_speaker_prefix():
    content = '[D1:1] Nora: A star means hope. A leaf means renewal.\n'
    catalog = build_evidence_catalog(chosen_for(content))
    first = {'facts': [{'kind': 'fact', 'text': 'A star means hope.',
        'evidence': [{'unit_id': 1, 'quote': 'A star means hope.', 'role': 'support'}], 'date_refs': []}]}
    review = queue(catalog, first)
    assert review['unreferenced_by_source'] == []
    row = review['partial_units'][0]
    assert row['unit_id'] == 1
    assert [content[slice(*span)].strip() for span in row['unquoted_spans']] == ['A leaf means renewal.']


def test_context_quote_is_not_reported_as_support_and_bad_draft_unit_adds_no_evidence():
    catalog = build_evidence_catalog(chosen_for('[D1:1] Nora: I may visit the museum.\n'))
    first = {'facts': [{'kind': 'limitation', 'text': 'The final plan is unknown.',
        'evidence': [{'unit_id': 1, 'quote': 'I may visit the museum.', 'role': 'context'},
            {'unit_id': 999, 'quote': 'invented', 'role': 'support'}], 'date_refs': []}]}
    review = queue(catalog, first)
    assert review['context_only_unit_ids'] == [1]
    assert '999' not in json.dumps(review)


def test_queue_keeps_all_units_without_a_fixed_list_window():
    content = ''.join(f'[D1:{index}] Nora: Planned independent event {index}.\n' for index in range(1, 302))
    catalog = build_evidence_catalog(chosen_for(content))
    assert queue(catalog)['unreferenced_by_source'] == [{'source_number': 1, 'unit_ids': list(range(1, 302))}]


@pytest.mark.parametrize('tail', ['She painted it that day.', '她当天画了这幅画。', 'The event date was 2023-08-23.'])
def test_review_queue_does_not_weaken_true_record_anchor_same_day_guard(material, tail):
    chosen, catalog, first = material
    first['facts'][0].update(kind='fact', text='Maya painted it last week relative to the 23 August 2023 session. ' + tail)
    with pytest.raises(ValueError, match='grounded_calendar_provenance_invalid'):
        reviewed_scope_answer(final_for(first, action='correct'), catalog, first,
            chosen=chosen, question='When?', compose_version='@31')


@pytest.mark.parametrize('refs', [[999], [1, 1]])
def test_new_review_queue_never_grants_invalid_date_reference_authority(material, refs):
    chosen, catalog, first = material
    first['facts'][0].update(kind='date', date_refs=refs, text='Maya painted it in 2023-08-14 to 2023-08-20.')
    with pytest.raises(ValueError, match='grounded_date_ref_invalid'):
        reviewed_scope_answer(final_for(first, action='correct'), catalog, first,
            chosen=chosen, question='When?', compose_version='@31')


def test_date_classification_candidates_only_use_actual_diagnostic_and_frozen_refs(material):
    chosen, catalog, first = material
    first['facts'][0].update(kind='limitation', text='The exact day is unknown, but it was in 2023-08-14 to 2023-08-20.')
    data = payload(build_partition_messages(chosen, 'When?', first_output=first, compose_version='@31'))
    assert data['review_queue']['date_classification_repairs'] == [{'fact_index': 1,
        'actual_kind': 'limitation', 'date_ref_ids': [1]}]
    assert data['untrusted_first_fact_plan'] == first
    assert data['first_fact_diagnostics'][0]['issues'][0]['actual_kind'] == 'limitation'


def test_related_dates_are_queued_even_for_valid_relative_only_draft(material):
    chosen, _, first = material
    data = payload(build_partition_messages(chosen, 'When?', first_output=first, compose_version='@31'))
    assert data['first_fact_diagnostics'] == []
    assert data['review_queue']['date_classification_repairs'] == []
    assert data['review_queue']['unreviewed_date_refs'] == [{'date_ref_id': 1, 'unit_id': 2}]


def test_queue_with_actual_schema_and_first_plan_fits_existing_ordinary16k_budget(material):
    chosen, _, first = material
    messages = build_partition_messages(chosen, 'When?', first_output=first, compose_version='@31')
    assert review_input_tokens(messages, final_response_model('@31', 'When?')) <= 4500


@pytest.mark.parametrize('kind', ['fact', 'inference', 'limitation'])
def test_date_queue_is_advisory_and_does_not_authorize_bad_event_date(material, kind):
    chosen, catalog, first = material
    first['facts'][0].update(kind=kind, text='Maya painted it on 2023-08-14.')
    before = deepcopy(first)
    _, _, diagnostics = inspect_referenced_first_draft(first, catalog, chosen=chosen, question='When?', compose_version='@31')
    assert diagnostics[0]['codes'] == ['grounded_calendar_provenance_invalid']
    assert first == before
    with pytest.raises(ValueError, match='grounded_calendar_provenance_invalid'):
        reviewed_scope_answer(final_for(first, action='correct'), catalog, first,
            chosen=chosen, question='When?', compose_version='@31')


def test_related_calculated_event_and_remaining_unknown_are_separate_legal_statements(material):
    chosen, catalog, first = material
    first['facts'][0].update(kind='limitation', text='The exact day is unknown, but it was in 2023-08-14 to 2023-08-20.')
    final = final_for(first, action='correct')
    final['statements'][0].update(kind='date', text='Maya painted it in 2023-08-14 to 2023-08-20.')
    limitation = deepcopy(final['statements'][0])
    limitation.update(kind='limitation', text='The exact day within that week is unknown.', date_refs=[], first_fact_uses=[])
    final['statements'].append(limitation)
    value = reviewed_scope_answer(final, catalog, first, chosen=chosen, question='When?', compose_version='@31')
    assert [item.kind for item in value.statements] == ['date', 'limitation']
    assert len(value.statements[0].derived_notes) == 1


def test_queue_is_deterministic_in_both_frozen_inputs_and_uses_unchanged_schema(material):
    chosen, _, first = material
    before = deepcopy((chosen, first))
    for final in [False, True]:
        args = {'first_output': first} if final else {}
        messages = build_partition_messages(chosen, 'What?', compose_version='@31', **args)
        assert messages == build_partition_messages(chosen, 'What?', compose_version='@31', **args)
        data = payload(messages)
        assert data['review_queue']['boundary'] == 'Mechanical locations only; relevance, completeness and claim modality require source review.'
    assert final_response_model('@31', 'What?') is final_response_model('@30', 'What?')
    assert (chosen, first) == before
    assert ACTIVE['compose'] == '@30'


@pytest.mark.parametrize('version', ['@13', '@14', '@15', '@16', '@17', '@18', '@19', '@20', '@21', '@22', '@23', '@24', '@25', '@26', '@27', '@28', '@29', '@30'])
def test_existing_instruction_versions_have_no_new_queue_contract(version):
    model = final_response_model(version, 'What?')
    text = get('compose', version=version)(lambda *a, **k: '', [], operation='partition_final_instruction',
        question='What?', response_schema=model.model_json_schema())
    assert 'review_queue' not in text
