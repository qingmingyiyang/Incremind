"""逐位置处置、真实替换和局部分句模态的32合同。"""
from copy import deepcopy

import pytest

from backend.memory_app.v2.answer_evidence import build_evidence_catalog, build_partition_messages, final_response_model, reviewed_scope_answer
from backend.memory_app.v2.policies import get
from tests.memory_app.v2.test_answer_contract_v31 import chosen_for, payload


def test_32_rebuild_input_omits_copyable_draft_text_but_preserves_frozen_original():
    chosen = chosen_for('[D1:1] Rin: It\'ll be tough as a parent, but I am ready!\n')
    first = {'facts': [{'kind': 'fact', 'text': 'Rin is a parent.', 'evidence': [
        {'unit_id': 1, 'quote': "It'll be tough as a parent, but I am ready!", 'role': 'support'}], 'date_refs': []}],
        'unreviewed': {'scope': 'all_unreferenced', 'reason': 'Rin is a parent.'}}
    before = deepcopy(first)
    data = payload(build_partition_messages(chosen, 'What is known?', first_output=first, compose_version='@32'))
    assert 'untrusted_first_fact_plan' not in data
    assert 'Rin is a parent.' not in str(data)
    assert data['first_evidence_plan'][0]['fact_index'] == 1
    assert first == before
    assert final_response_model('@32', 'What?').__name__ == 'ReconstructedReviewedOutput'


def test_32_unit_review_and_specific_detail_have_distinct_coverage_semantics():
    catalog = build_evidence_catalog(chosen_for('[D1:1] Rin: A gift. [Image caption: a star and a leaf]\n'))
    queue = get('compose', version='@32')(None, operation='review_queue', catalog=catalog)
    assert {item['scope'] for item in queue['items']} == {'unit_review', 'span_review'}
    assert len({item['item_id'] for item in queue['items']}) == len(queue['items'])
    for item in queue['items']:
        text = catalog['units'][item['unit_id'] - 1]['text']
        assert item['preview'] == text[slice(*item['preview_span'])]
        assert item['span'][0] <= item['preview_span'][0] < item['preview_span'][1] <= item['span'][1]


def test_32_modal_clauses_do_not_let_neighbor_question_weaken_assertion():
    catalog = build_evidence_catalog(chosen_for('[D1:1] Rin: Blue means peace. Flowers mean hope, right? I will arrange a concert, but I already joined a club.\n'))
    clauses = get('compose', version='@32')(None, operation='modal_clauses', text=catalog['units'][0]['text'])
    assert [(row['text'].strip(), row['stance']) for row in clauses] == [
        ('Blue means peace.', 'asserted'), ('Flowers mean hope, right?', 'question'),
        ('I will arrange a concert,', 'planned'), ('but I already joined a club.', 'asserted')]


def first_for(quote, *, text='Rin described the event.', kind='fact', refs=None):
    return {'facts': [{'kind': kind, 'text': text, 'evidence': [{'unit_id': 1, 'quote': quote, 'role': 'support'}],
        'date_refs': refs or []}], 'unreviewed': {'scope': 'all_unreferenced', 'reason': 'Initial draft only.'}}


def final_for(catalog, first, *, action='adopt', stance='asserted', changed=None, date_refs=()):
    fact = {**deepcopy(first['facts'][0]), **(changed or {})}
    anchors = []
    for evidence in fact['evidence']:
        text = catalog['units'][evidence['unit_id'] - 1]['text']
        for row in get('compose', version='@32')(None, operation='modal_clauses', text=text):
            if row['text'] in evidence['quote']:
                anchors.append({'unit_id': evidence['unit_id'], 'quote': row['text']})
    fact.update(first_fact_uses=[{'fact_index': 1, 'action': action, 'reason': 'Reviewed the actual statement fields.'}],
        stance=stance, source_clauses=anchors)
    queue = get('compose', version='@32')(None, operation='review_queue', catalog=catalog,
        first_output=first, date_refs=date_refs)
    return {'statements': [fact], 'unused': {'scope': 'all_unreferenced', 'reason': 'Only requested facts.'},
        'uncertain': [], 'excluded_facts': [], 'review_dispositions': [
            {'item_ids': [row['item_id'] for row in queue['items']], 'action': 'exclude',
             'statement_indices': [], 'reason': 'Other source positions do not add a requested claim.'}] if queue['items'] else []}


def verify(output, catalog, first, chosen):
    return reviewed_scope_answer(output, catalog, first, chosen=chosen, question='What is known? Answer in English.', compose_version='@32')


@pytest.mark.parametrize(('text', 'states'), [
    ('I love drawing and hope to exhibit next year.', ['asserted', 'planned']),
    ('I do not plan to move.', ['asserted']),
    ("I hope you're well. I attended the event.", ['asserted', 'asserted']),
    ('I plan to teach a workshop and write a handbook.', ['planned', 'planned']),
    ('I will mentor students and organize a festival.', ['planned', 'planned']),
    ('Next month, I plan to host a workshop.', ['planned']),
    ('蓝色让我放松。向日葵代表温暖吗？', ['asserted', 'question']),
    ('I will teach and she already joined a club.', ['planned', 'asserted']),
])
def test_local_modal_controls(text, states):
    assert [row['stance'] for row in get('compose', version='@32')(None,
        operation='modal_clauses', text=text)] == states


def test_plan_is_not_published_as_current_fact_and_cannot_hide_cue_in_another_anchor():
    quote = "It'll be tough as a parent, but I am ready!"
    chosen = chosen_for('[D1:1] Rin: ' + quote + '\n')
    catalog = build_evidence_catalog(chosen)
    first = first_for(quote, text='Rin is a parent.')
    output = final_for(catalog, first)
    with pytest.raises(ValueError, match='modal_source_stance_mismatch'):
        verify(output, catalog, first, chosen)
    output['statements'][0]['source_clauses'] = [{'unit_id': 1, 'quote': 'as a parent'}]
    with pytest.raises(ValueError, match='modal_source_clause_invalid'):
        verify(output, catalog, first, chosen)
    output = final_for(catalog, first, action='correct', stance='planned', changed={'text': 'Rin expects parenting to be challenging.'})
    result = verify(output, catalog, first, chosen)
    assert result.statements[0].text.startswith('Planned or hoped: ')


def test_inference_can_use_plan_but_must_really_present_its_qualified_premise():
    quote = 'I plan to become a parent.'
    chosen = chosen_for('[D1:1] Rin: ' + quote + '\n')
    catalog = build_evidence_catalog(chosen)
    first = first_for(quote)
    output = final_for(catalog, first, action='correct', stance='inference',
        changed={'kind': 'inference', 'text': 'Parenthood is a future intention.'})
    result = verify(output, catalog, first, chosen)
    assert 'Based on a stated plan, without confirmation of current status:' in result.statements[0].text
    output['statements'][0]['kind'] = 'fact'
    with pytest.raises(ValueError, match='modal_inference_kind_invalid'):
        verify(output, catalog, first, chosen)


def test_visible_planned_qualification_is_a_real_correction_even_when_core_proposition_is_unchanged():
    quote = 'I plan to become a parent.'
    chosen = chosen_for('[D1:1] Rin: ' + quote + '\n')
    catalog = build_evidence_catalog(chosen)
    first = first_for(quote, text='Rin becomes a parent.')
    result = verify(final_for(catalog, first, action='correct', stance='planned'), catalog, first, chosen)
    assert result.statements[0].text == 'Planned or hoped: Rin becomes a parent.'


def test_affirmative_local_quote_survives_neighbor_question_and_future_plan():
    chosen = chosen_for('[D1:1] Rin: Blue means peace. Flowers mean hope, right? I will host a concert.\n')
    catalog = build_evidence_catalog(chosen)
    first = first_for('Blue means peace.', text='Blue means peace.')
    result = verify(final_for(catalog, first), catalog, first, chosen)
    assert result.statements[0].text == 'Blue means peace.'


@pytest.mark.parametrize(('anchor', 'stance', 'prefix'), [
    ('I love blue.', 'asserted', ''), ('Flowers mean hope, right?', 'question', 'Unconfirmed interpretation: '),
])
def test_one_long_quote_can_retain_context_without_letting_neighbor_modality_change_claim(anchor, stance, prefix):
    quote = 'I love blue. Flowers mean hope, right?'
    chosen = chosen_for('[D1:1] Rin: ' + quote + '\n')
    catalog = build_evidence_catalog(chosen)
    first = first_for(quote)
    output = final_for(catalog, first, action='correct', stance=stance, changed={'text': anchor})
    output['statements'][0]['source_clauses'] = [{'unit_id': 1, 'quote': anchor}]
    assert verify(output, catalog, first, chosen).statements[0].text == prefix + anchor


@pytest.mark.parametrize('fault', ['missing', 'duplicate', 'unknown'])
def test_queue_positions_must_be_disposed_exactly_once(fault):
    chosen = chosen_for('[D1:1] Rin: A star means hope. A leaf means renewal.\n')
    catalog = build_evidence_catalog(chosen)
    first = first_for('A star means hope.')
    output = final_for(catalog, first)
    ids = output['review_dispositions'][0]['item_ids']
    if fault == 'missing':
        output['review_dispositions'] = []
    elif fault == 'duplicate':
        ids.append(ids[0])
    else:
        ids.append(999)
    with pytest.raises(ValueError, match='review_item_coverage_invalid'):
        verify(output, catalog, first, chosen)


@pytest.mark.parametrize('fault', ['other_clause', 'context', 'partial', 'absent_statement'])
def test_specific_span_inclusion_requires_real_same_unit_support_covering_target(fault):
    chosen = chosen_for('[D1:1] Rin: A star means hope. [Image caption: a star and a leaf]\n')
    catalog = build_evidence_catalog(chosen)
    first = first_for('A star means hope.')
    output = final_for(catalog, first)
    queue = get('compose', version='@32')(None, operation='review_queue', catalog=catalog, first_output=first)
    item = next(row for row in queue['items'] if 'caption_spans' in row['tags'])
    all_ids = [row['item_id'] for row in queue['items'] if row['item_id'] != item['item_id']]
    output['review_dispositions'] = [{'item_ids': [item['item_id']], 'action': 'include', 'statement_indices': [1],
        'reason': 'The visible component is part of the requested symbol.'}]
    if all_ids:
        output['review_dispositions'].append({'item_ids': all_ids, 'action': 'exclude', 'statement_indices': [], 'reason': 'Other detail.'})
    if fault == 'context':
        output['statements'][0]['evidence'].append({'unit_id': 1, 'quote': '[Image caption: a star and a leaf]', 'role': 'context'})
    elif fault == 'partial':
        output['statements'][0]['evidence'].append({'unit_id': 1, 'quote': 'a star and a leaf', 'role': 'support'})
    elif fault == 'absent_statement':
        output['review_dispositions'][0]['statement_indices'] = [2]
    with pytest.raises(ValueError, match='review_inclusion_span_not_supported|review_statement_position_invalid'):
        verify(output, catalog, first, chosen)


def test_correct_reason_does_not_make_unchanged_fields_a_replacement():
    chosen = chosen_for('[D1:1] Rin: I joined the club.\n')
    catalog = build_evidence_catalog(chosen)
    first = first_for('I joined the club.')
    with pytest.raises(ValueError, match='first_fact_replacement_unchanged'):
        verify(final_for(catalog, first, action='correct'), catalog, first, chosen)


def test_unit_review_accepts_a_short_real_excerpt_without_claiming_whole_unit_coverage():
    chosen = chosen_for('[D1:1] Rin: I joined the club. The weather is pleasant.\n')
    catalog = build_evidence_catalog(chosen)
    first = {'facts': [], 'unreviewed': {'scope': 'all_unreferenced', 'reason': 'No draft.'}}
    fact = first_for('I joined the club.')['facts'][0]
    output = final_for(catalog, first_for('I joined the club.'))
    output['statements'][0]['first_fact_uses'] = []
    queue = get('compose', version='@32')(None, operation='review_queue', catalog=catalog, first_output=first)
    output['review_dispositions'] = [{'item_ids': [queue['items'][0]['item_id']], 'action': 'include',
        'statement_indices': [1], 'reason': 'The club participation answers the question.'}]
    assert len(fact['evidence'][0]['quote']) < len(catalog['units'][0]['text'])
    assert verify(output, catalog, first, chosen).statements[0].text == fact['text']


def date_case(*, kind='fact', text='Rin celebrated graduation on 2023-08-13.'):
    from backend.memory_app.v2.policies.date_grounding import build_date_ref_catalog
    quote = 'Last night was great! We celebrated graduation.'
    chosen = chosen_for('[D1:1] Rin: ' + quote + '\n')
    chosen[0]['title'] = 'Session 2023-08-14'
    catalog = build_evidence_catalog(chosen)
    refs = build_date_ref_catalog(catalog, chosen)
    assert len(refs) == 1
    first = first_for(quote, kind=kind, text=text, refs=[refs[0]['date_ref_id']])
    return chosen, catalog, refs, first


def test_true_date_classification_repair_can_change_kind_only_without_rewriting_legal_text():
    chosen, catalog, refs, first = date_case()
    output = final_for(catalog, first, action='correct', changed={'kind': 'date'}, date_refs=refs)
    result = verify(output, catalog, first, chosen)
    assert result.statements[0].text == first['facts'][0]['text']
    assert result.statements[0].kind == 'date'


@pytest.mark.parametrize('fault', ['clear_refs_only', 'wrong_date', 'unknown_ref'])
def test_calendar_still_rejects_reason_only_mixed_unknown_or_unbound_date(fault):
    chosen, catalog, refs, first = date_case(kind='limitation',
        text='The exact graduation date is unknown, but the celebration was on 2023-08-13.')
    if fault == 'clear_refs_only':
        changed = {'date_refs': []}
    elif fault == 'wrong_date':
        changed = {'kind': 'date', 'text': 'Rin celebrated graduation on 2023-08-12.'}
    else:
        changed = {'kind': 'date', 'text': 'Rin celebrated graduation on 2023-08-13.', 'date_refs': [999]}
    output = final_for(catalog, first, action='correct', changed=changed, date_refs=refs)
    with pytest.raises(ValueError, match='grounded_calendar_provenance_invalid|grounded_date_ref_invalid'):
        verify(output, catalog, first, chosen)


def test_known_related_date_and_unknown_target_are_separate_actual_statements():
    chosen, catalog, refs, first = date_case(kind='limitation',
        text='The exact graduation date is unknown, but the celebration was on 2023-08-13.')
    output = final_for(catalog, first, action='correct',
        changed={'kind': 'date', 'text': 'Rin celebrated graduation on 2023-08-13.'}, date_refs=refs)
    unknown = deepcopy(output['statements'][0])
    unknown.update(kind='limitation', text='The exact graduation date is not stated.', date_refs=[],
        first_fact_uses=[], stance='unknown')
    output['statements'].append(unknown)
    result = verify(output, catalog, first, chosen)
    assert [(row.kind, row.text) for row in result.statements] == [
        ('date', 'Rin celebrated graduation on 2023-08-13.'), ('limitation', 'The exact graduation date is not stated.')]


def test_output_minimum_counts_all_items_without_truncating_them():
    from backend.memory_app.v2.answer_evidence import grounded_output_minimum
    catalog = build_evidence_catalog(chosen_for(''.join(f'[D1:{i}] Rin: Independent event {i}.\n' for i in range(1, 1402))))
    queue = get('compose', version='@32')(None, operation='review_queue', catalog=catalog)
    assert len(queue['items']) == 1401
    assert grounded_output_minimum(catalog, compose_version='@32', review_queue=queue) > 7000


def test_final_preserves_existing_temporal_and_supplemented_method_rules():
    chosen = chosen_for('[D1:1] Rin: I learned a method.\n')
    chosen[0].update(temporal=True, supplemented=True)
    mandatory = get('compose', version='@3')(lambda *a, **k: '', chosen, operation='instruction')
    instruction = get('compose', version='@32')(None, chosen, operation='partition_final_instruction',
        response_schema=final_response_model('@32', 'What?').model_json_schema())
    assert mandatory and instruction.endswith(mandatory)
    assert '情境补全方法只在适用条件满足时参考' in instruction


def test_caption_remains_one_clause_even_when_its_components_use_and():
    text = '[D1:1] Rin: A gift. [Image caption: a star and a leaf] It means family support.'
    clauses = get('compose', version='@32')(None, operation='modal_clauses', text=text)
    assert [row['text'] for row in clauses] == ['A gift.', '[Image caption: a star and a leaf]', 'It means family support.']


def test_full_sentence_anchor_can_preserve_nominal_conjunction_without_arithmetic_offsets():
    quote = 'The symbol means strength and resilience.'
    chosen = chosen_for('[D1:1] Rin: ' + quote + '\n')
    catalog = build_evidence_catalog(chosen)
    first = first_for(quote, text='The symbol means strength and resilience.')
    output = final_for(catalog, first)
    output['statements'][0]['source_clauses'] = [{'unit_id': 1, 'quote': quote}]
    assert verify(output, catalog, first, chosen).statements[0].text == first['facts'][0]['text']


def test_stance_render_uses_existing_question_language_rule():
    render = get('compose', version='@32')(None, operation='stance_render', question='有什么计划？')
    assert render['prefixes']['planned'] == '计划或期望：'
    assert '当前状态仍未确认' in render['inference_premise']['planned']


def test_inclusion_cannot_pad_statement_indices_with_an_unrelated_same_unit_claim():
    chosen = chosen_for('[D1:1] Rin: I joined the club. The weather is pleasant.\n')
    catalog = build_evidence_catalog(chosen)
    first = first_for('I joined the club.')
    output = final_for(catalog, first)
    other = deepcopy(output['statements'][0])
    other.update(text='The weather is pleasant.', first_fact_uses=[],
        evidence=[{'unit_id': 1, 'quote': 'The weather is pleasant.', 'role': 'support'}],
        source_clauses=[{'unit_id': 1, 'quote': 'The weather is pleasant.'}])
    output['statements'].append(other)
    output['review_dispositions'][0].update(action='include', statement_indices=[1, 2])
    with pytest.raises(ValueError, match='review_inclusion_statement_not_supported'):
        verify(output, catalog, first, chosen)


@pytest.mark.parametrize('quote', ['I missed my friends.', 'I missed my family.', 'I missed my home.'])
def test_emotional_missing_does_not_render_as_an_unattended_event(quote):
    chosen = chosen_for('[D1:1] Rin: ' + quote + '\n')
    catalog = build_evidence_catalog(chosen)
    first = first_for(quote, text=quote)
    assert verify(final_for(catalog, first), catalog, first, chosen).statements[0].text == quote
    with pytest.raises(ValueError, match='modal_source_stance_mismatch'):
        verify(final_for(catalog, first, stance='missed'), catalog, first, chosen)


@pytest.mark.parametrize('quote', ['I missed the parade.', 'I missed the conference.', 'I did not attend the conference.'])
def test_explicit_event_absence_keeps_its_visible_qualification(quote):
    chosen = chosen_for('[D1:1] Rin: ' + quote + '\n')
    catalog = build_evidence_catalog(chosen)
    first = first_for(quote, text=quote)
    with pytest.raises(ValueError, match='modal_source_stance_mismatch'):
        verify(final_for(catalog, first), catalog, first, chosen)
    result = verify(final_for(catalog, first, action='correct', stance='missed'), catalog, first, chosen)
    assert result.statements[0].text == 'Not attended or cancelled: ' + quote


def test_ambiguous_missing_pronoun_does_not_force_an_event_interpretation():
    clauses = get('compose', version='@32')(None, operation='modal_clauses', text='I missed it.')
    assert clauses[0]['stance'] == 'asserted'
