"""记录锚上下文不提供事件日期资格；原始模型字段保持不变。"""
from copy import deepcopy

import pytest

from backend.memory_app.answer_proof import review_input_tokens
from backend.memory_app.v2.answer_evidence import build_evidence_catalog, build_partition_messages, final_response_model, inspect_referenced_first_draft, reviewed_scope_answer
from backend.memory_app.v2.policies import ACTIVE
from core.search_and_recall.evidence_windows import EvidenceWindow
from tests.memory_app.v2.test_answer_contract_v27 import material as _material_fixture
from tests.memory_app.v2.test_answer_contract_v28 import final_for

material = _material_fixture


@pytest.mark.parametrize('refs', [[], [1]])
def test_explicit_record_anchor_and_repeated_relative_week_remain_context_only(material, refs):
    chosen, catalog, first = material
    first['facts'][0].update(kind='fact', date_refs=refs,
        text='Maya painted it last week relative to the 23 August 2023 conversation, in the week before 23 August 2023.')
    before = deepcopy(first)
    _, normalized, diagnostics = inspect_referenced_first_draft(first, catalog, chosen=chosen, question='What?', compose_version='@30')
    assert diagnostics == [] and normalized.facts[0].derived_notes == []
    assert first == before
    result = reviewed_scope_answer(final_for(first), catalog, first, chosen=chosen, question='What?', compose_version='@30')
    assert result.statements[0].derived_notes == []
    _, _, previous = inspect_referenced_first_draft(first, catalog, chosen=chosen, question='What?', compose_version='@29')
    assert previous[0]['codes'] == ['grounded_calendar_provenance_invalid']


@pytest.mark.parametrize('text', [
    'Maya painted it last week relative to the 24 August 2023 conversation.',
    'Maya painted it on 23 August 2023.',
    'Relative to the 23 August 2023 conversation, Maya painted it on 23 August 2023.',
    'Relative to the 23 August 2023 conversation, Maya painted it that day.',
    'Relative to the 23 August 2023 conversation, Maya painted it on the same day.',
    'Maya painted it last week relative to the 23 August 2023 conversation. She painted it on the same date.',
    'Maya painted it last week relative to the 23 August 2023 conversation. She painted it on this date.',
    'Maya painted it last week relative to the 23 August 2023 conversation. She painted it the day of that conversation.',
    'Maya painted it last week relative to the 23 August 2023 conversation. She painted it on the recorded date.',
    'Maya painted it last week relative to the 23 August 2023 conversation. She painted it at that time.',
    'Maya painted it last week relative to the 23 August 2023 conversation. She painted it then.',
    'Relative to the 23 August 2023 conversation, Maya painted it in 2023-08-14 to 2023-08-20.',
    'Maya painted it in the week before 23 August 2023.',
])
def test_context_anchor_does_not_authorize_wrong_event_ambiguous_day_or_ranges(material, text):
    chosen, catalog, first = material
    first['facts'][0].update(kind='fact', text=text)
    _, normalized, diagnostics = inspect_referenced_first_draft(first, catalog, chosen=chosen, question='When?', compose_version='@30')
    assert diagnostics[0]['codes'] == ['grounded_calendar_provenance_invalid']
    assert diagnostics[0]['issues'][0]['actual_kind'] == 'fact'
    assert diagnostics[0]['issues'][0]['date_refs_grant_calculation'] is False
    assert normalized.facts[0].derived_notes == []
    with pytest.raises(ValueError, match='grounded_calendar_provenance_invalid'):
        reviewed_scope_answer(final_for(first, action='correct'), catalog, first, chosen=chosen, question='When?', compose_version='@30')


def test_record_anchor_cannot_authorize_later_precision_limitation(material):
    chosen, catalog, first = material
    first['facts'][0].update(kind='fact', text='Maya painted it last week relative to the 23 August 2023 session.')
    limitation = deepcopy(first['facts'][0])
    limitation.update(kind='limitation', text='2023-08-14 to 2023-08-20 is only a week range.')
    first['facts'].append(limitation)
    _, normalized, diagnostics = inspect_referenced_first_draft(first, catalog, chosen=chosen, question='When?', compose_version='@30')
    assert [item['fact_index'] for item in diagnostics] == [2]
    assert normalized.facts[0].derived_notes == []


def test_context_only_anchor_requires_the_complete_real_sentence(material):
    chosen, catalog, first = material
    first['facts'][0].update(kind='fact', date_refs=[],
        text='Maya painted it relative to the 23 August 2023 conversation.')
    first['facts'][0]['evidence'][0]['quote'] = 'I painted this self-portrait'
    with pytest.raises(ValueError, match='grounded_calendar_provenance_invalid'):
        reviewed_scope_answer(final_for(first, action='correct'), catalog, first, chosen=chosen, question='What?', compose_version='@30')


def test_new_diagnostic_and_anchor_instruction_fit_ordinary16k_and_select_current_defaults(material):
    chosen, _, first = material
    first['facts'][0].update(kind='fact', text='Maya painted it in the week before 23 August 2023.')
    messages = build_partition_messages(chosen, 'When?', first_output=first, compose_version='@30')
    assert review_input_tokens(messages, final_response_model('@30', 'When?')) <= 4500
    assert {name: ACTIVE[name] for name in ('compose', 'ladder', 'retrieve', 'rerank')} == {
        'compose': '@30', 'ladder': '@15', 'retrieve': '@5', 'rerank': '@2'}


@pytest.mark.parametrize('event', ['', '她当天画了这幅画。', '她于2023年8月23日画了这幅画。',
    '她在这一天画了这幅画。', '她在该日画了这幅画。', '她在那一天画了这幅画。',
    '她在会话日期画了这幅画。', '她在那一日画了这幅画。', '她在那个时候画了这幅画。'])
def test_chinese_record_context_does_not_authorize_same_day_event(material, event):
    chosen, catalog, first = material
    first['facts'][0].update(kind='fact', text='Maya说上周画了这幅画，以2023年8月23日的会话日期为参照。' + event)
    if event:
        with pytest.raises(ValueError, match='grounded_calendar_provenance_invalid'):
            reviewed_scope_answer(final_for(first, action='correct'), catalog, first, chosen=chosen, question='什么时候？', compose_version='@30')
    else:
        result = reviewed_scope_answer(final_for(first), catalog, first, chosen=chosen, question='什么时候？', compose_version='@30')
        assert result.statements[0].derived_notes == []


@pytest.mark.parametrize('event', ['She painted it that day.', 'She painted it on the same date.',
    'She painted it on the recorded date.', '她在这一天画了这幅画。'])
def test_valid_date_range_cannot_hide_ambiguous_same_day_event(material, event):
    chosen, catalog, first = material
    first['facts'][0].update(kind='date', text='Maya painted it relative to the 23 August 2023 session date: 2023-08-14 to 2023-08-20. ' + event)
    with pytest.raises(ValueError, match='grounded_calendar_provenance_invalid'):
        reviewed_scope_answer(final_for(first, action='correct'), catalog, first, chosen=chosen, question='When?', compose_version='@30')


@pytest.mark.parametrize('kind', ['fact', 'inference'])
@pytest.mark.parametrize('refs', [[], [1]])
def test_remaining_time_reference_has_no_authority_for_either_ordinary_kind(material, kind, refs):
    chosen, catalog, first = material
    first['facts'][0].update(kind=kind, date_refs=refs,
        text='Maya painted it last week relative to the 23 August 2023 conversation. She painted it at that time.')
    with pytest.raises(ValueError, match='grounded_calendar_provenance_invalid'):
        reviewed_scope_answer(final_for(first, action='correct'), catalog, first, chosen=chosen, question='When?', compose_version='@30')


def test_independent_source_explicit_event_day_keeps_its_original_authority(material):
    chosen, _, first = material
    quote = 'I painted this self-portrait on 23 August 2023.'
    content = 'Session date: 2023-08-23\n[D1:1] Maya: ' + quote + '\n'
    chosen[0].update(excerpt=content, entry={'content': content}, windows=(EvidenceWindow(0, len(content), content),))
    catalog = build_evidence_catalog(chosen)
    unit = next(unit for unit in catalog['units'] if quote in unit['text'])
    first['facts'][0].update(kind='fact', date_refs=[],
        text='Relative to the 23 August 2023 conversation, Maya painted it on the same date.',
        evidence=[{'unit_id': unit['id'], 'quote': quote, 'role': 'support'}])
    result = reviewed_scope_answer(final_for(first), catalog, first, chosen=chosen, question='When?', compose_version='@30')
    assert result.statements[0].derived_notes == []


@pytest.mark.parametrize('text', [
    'Maya painted it relative to the 23 August 2023 session date: 2023-08-14 to 2023-08-20.',
    'Maya painted it relative to the 23 August 2023 session date: 2023-08-14.',
])
def test_date_kind_complete_range_remains_strict(material, text):
    chosen, catalog, first = material
    first['facts'][0].update(kind='date', text=text)
    if 'to 2023-08-20' in text:
        assert reviewed_scope_answer(final_for(first), catalog, first, chosen=chosen, question='When?', compose_version='@30').statements
    else:
        with pytest.raises(ValueError, match='grounded_calendar_provenance_invalid'):
            reviewed_scope_answer(final_for(first, action='correct'), catalog, first, chosen=chosen, question='When?', compose_version='@30')
