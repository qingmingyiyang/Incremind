from copy import deepcopy

import pytest

from backend.memory_app.answer_proof import review_input_tokens
from backend.memory_app.source_egress import SourceEgressService
from backend.memory_app.v2.answer_evidence import answer_response_model, build_partition_messages
from backend.memory_app.v2.contextual_chunks import dynamic_contextual_windows
from backend.memory_app.v2.part_context import attachment_text, fit_attachment_context
from backend.memory_app.v2.policies import version
from backend.recognition import RecognitionConflict, WorkScope
from core.storage_provider import SQLiteStructuredRecordStore


QUESTION = '对比各份访谈的预算和交付分歧'


def context_for(records):
    scope = WorkScope('local-user', 'alpha')
    body = ('甲方预算十万元，五月十日交付。公开访谈保留预算说明。\n\n'
            '乙方预算十二万元，五月二十日交付。\n\n' + '项目资料。' * 400)
    with records.begin() as tx:
        tx.put('v2_turns', 'parent', {'intent': 'multi', 'project_id': 'alpha', 'part_turn_ids': []}, expected_revision=0)
        for index in range(3):
            identity = f'original-{index}'
            tx.put('workspace_items', identity, {'id': identity, 'project_id': 'alpha', 'input_kind': 'text',
                'title': f'公开访谈{index}', 'source_text': body, 'status': 'ready',
                'created_at': '2026-10-25T00:00:00Z'}, expected_revision=0)
        tx.commit()
    authority = SourceEgressService(records)
    originals = [authority.snapshot_original_content(scope, f'original-{index}') for index in range(3)]
    context = {'project_id': 'alpha', 'parent': 'parent', 'privacy_revision': 0, 'originals': originals,
        'answers': [], 'attachment_titles': [f'公开访谈{index}' for index in range(3)]}
    context['text'] = attachment_text(context)
    return context


def cost(context):
    selected = version('compose')
    return review_input_tokens(build_partition_messages([], QUESTION, part_context=context,
        compose_version=selected), response_model=answer_response_model(selected))


def reserve_context(context):
    reserved = deepcopy(context)
    for index, original in enumerate(context['originals']):
        candidates = []
        def observe(selection):
            candidates.append(selection)
            return False
        dynamic_contextual_windows(original['content']['identity']['source_text'], QUESTION,
            title=context['attachment_titles'][index], admission=observe)
        smallest = min(candidates, key=lambda selection: len(selection.excerpt))
        reserved['originals'][index]['content']['windows'] = [
            {'start': window.start, 'end': window.end, 'text': window.text} for window in smallest.windows]
    reserved['text'] = attachment_text(reserved)
    return reserved


def test_all_long_attachments_keep_legal_evidence_in_the_real_message_budget(tmp_path):
    records = SQLiteStructuredRecordStore(tmp_path / 'records.sqlite3')
    context = context_for(records)
    minimum = reserve_context(context)
    budget = cost(minimum)
    assert cost(context) > budget
    selected = fit_attachment_context(records, None, context, QUESTION, lambda value: cost(value) <= budget)
    assert cost(selected) <= budget
    assert len(selected['originals']) == 3
    for original in selected['originals']:
        windows = original['content']['windows']
        assert windows
        body = original['content']['identity']['source_text']
        assert all(window['text'] == body[window['start']:window['end']] for window in windows)


def test_minimum_legal_evidence_over_budget_fails_without_sending(tmp_path):
    records = SQLiteStructuredRecordStore(tmp_path / 'records.sqlite3')
    context = context_for(records)
    budget = cost(reserve_context(context)) - 1
    with pytest.raises(RecognitionConflict):
        fit_attachment_context(records, None, context, QUESTION, lambda value: cost(value) <= budget)
