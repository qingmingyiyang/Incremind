"""统一owner使用真实纯策略，模型只隔离为可检查的阶段传输。"""
from copy import deepcopy
from dataclasses import replace
import json

import pytest

from backend.memory_app.v2.context_preparation import prepare_context, read_context, reduce_summaries
from backend.memory_app.v2.policies import get
from backend.memory_app.v2.policies.context_preparation import ContextPreparationInput, ContextSpan


def request(text, *, limit=1800):
    span = ContextSpan('body', {'type': 'original_item', 'id': 'test', 'revision': 1}, 'source_v1', 0, len(text), text)
    return ContextPreparationInput('分析材料中的条件和限制', '依据原文分析。',
        [{'role': 'user', 'content': text}], span, (), (span,), limit, 7000)


class SummaryTransport:
    def __init__(self):
        self.calls = []
        self.reject = False

    def __call__(self, stage, messages, parser):
        self.calls.append((stage, deepcopy(messages)))
        values = json.loads(messages[-1]['content'])
        if '-audit-' in stage:
            after = [item for summary in values['after'] for item in summary['items']]
            value = {'checks': [{'id': item['id'], 'verdict': 'changed' if self.reject else 'preserved',
                'reason': '遗漏条件' if self.reject else '已比较原文与摘要',
                'original_quote': (values['source']['text'] if 'source_key' in item else
                    item['evidence'][0].get('quote', values['source']['text']))[:10],
                'summary_evidence': [{'index': 0, 'quote': after[0]['text']}]}
                for item in values['protected']]}
        elif 'source' in values:
            span = values['source']
            quote = span['text'].strip()[:20]
            start = span['start'] + span['text'].index(quote)
            value = {'coverage': [span['key']], 'items': [{'kind': 'condition', 'text': '必须先检查条件',
                'evidence': [{'key': span['key'], 'start': start, 'end': start + len(quote), 'quote': quote}]}]}
        else:
            value = {'coverage': values['coverage'], 'items': [item for summary in values['summaries'] for item in summary['items']]}
        return parser(json.dumps(value, ensure_ascii=False))


def test_registry_dispatch_and_original_fast_path_zero_model_calls():
    policy = get('context_preparation', version='@1')
    assert policy('fits', [{'role': 'user', 'content': '短文'}], 100)
    with pytest.raises(ValueError, match='operation_invalid'):
        policy('__dict__')
    text = '完整原件无需压缩。'
    source, transport = request(text), SummaryTransport()
    result = prepare_context(source, complete_stage=transport,
        validate_current=lambda: None, read_original=lambda ref: text)
    assert not result.compressed and not transport.calls
    assert result.parts[0].messages == list(source.original_messages)


def test_actual_map_reduce_focus_audit_and_read_original_scope():
    text = '\n\n'.join('第' + str(index) + '段。如果具备条件，才允许执行；条件不足时保留未知。' for index in range(160))
    source, transport = request(text), SummaryTransport()
    result = prepare_context(source, complete_stage=transport,
        validate_current=lambda: None, read_original=lambda ref: text)
    assert result.compressed and len(result.parts) > 1
    assert any('-map-' in stage for stage, _ in transport.calls)
    assert any('-reduce-' in stage for stage, _ in transport.calls)
    assert any('-audit-' in stage for stage, _ in transport.calls)
    assert ''.join(part.focus.text for part in result.parts) == text
    assert all(get('context_preparation').fits(part.messages, source.input_limit) for part in result.parts)
    assert tuple(result.structure[0]['coverage']) == result.coverage
    ranges = [{'key': 'body', 'start': 0, 'end': 10, 'reason': '核对原文条件'}]
    found = read_context(source, ranges, read_original=lambda ref: text, validate_current=lambda: None)
    assert found[0].text == text[:10]
    assert len(read_context(source, ranges + ranges,
        read_original=lambda ref: text, validate_current=lambda: None)) == 1
    with pytest.raises(ValueError, match='read_invalid'):
        read_context(source, [{**ranges[0], 'key': 'outside'}], read_original=lambda ref: text, validate_current=lambda: None)


def test_semantic_audit_rejects_condition_loss_even_with_valid_coordinates():
    text = '\n\n'.join('条件' + str(index) + '成立才执行。未知信息需要核对原文。' for index in range(160))
    source, transport = request(text), SummaryTransport()
    transport.reject = True
    with pytest.raises(ValueError, match='summary_semantics_invalid'):
        prepare_context(source, complete_stage=transport,
            validate_current=lambda: None, read_original=lambda ref: text)
    assert any('-audit-' in stage for stage, _ in transport.calls)


def test_source_revocation_stops_before_dispatch():
    source, transport = request('必须保持真实原文。'), SummaryTransport()
    with pytest.raises(ValueError, match='source_changed'):
        prepare_context(source, complete_stage=transport,
            validate_current=lambda: None, read_original=lambda ref: '已被修改。')
    assert not transport.calls


def test_apostrophe_does_not_split_quoted_argument():
    text = "Intro. He said 'Don't split. Keep both.' Final observation."
    span = request(text).focus
    parts = get('context_preparation').units(span, fits=lambda part: len(part.text) <= 39)
    assert any("'Don't split. Keep both.'" in part.text for part in parts)
    assert ''.join(part.text for part in parts) == text


def test_overlapping_surrounding_rejected_but_focus_can_overlap_full_body():
    text = '完整原文包含条件以及条件不足时的未知。'
    source, transport = request(text), SummaryTransport()
    overlapping = replace(source.focus, key='overlap', start=2, end=8, text=text[2:8])
    source = replace(source, surrounding=(source.focus, overlapping))
    with pytest.raises(ValueError, match='source_overlap'):
        prepare_context(source, complete_stage=transport, validate_current=lambda: None, read_original=lambda ref: text)
    assert not transport.calls


def test_audit_message_keeps_one_source_copy_and_parser_checks_original_quote():
    text = ('The condition applies only while the stated requirements remain satisfied and uncertainty remains explicit, ' * 22) + '.'
    source, policy = request(text, limit=1600), get('context_preparation')
    after = [{'coverage': ['body'], 'items': [{'kind': 'condition', 'text': 'Conditions and uncertainty remain explicit.',
        'evidence': [{'key': 'body', 'start': 0, 'end': len(text), 'quote': text}]}]}]
    messages = policy.audit_messages(source, source.focus, before=[], after=after)
    values = json.loads(messages[-1]['content'])
    assert messages[-1]['content'].count(text) == 1
    assert values['protected'][0] == {'id': 'source:0:' + str(len(text)), 'source_key': 'body', 'start': 0, 'end': len(text)}
    assert policy.fits(messages, source.input_limit)
    raw = {'checks': [{'id': values['protected'][0]['id'], 'verdict': 'preserved', 'reason': '逐项核验',
        'original_quote': text[:20], 'summary_evidence': [{'index': 0, 'quote': after[0]['items'][0]['text']}]}]}
    assert policy.parse_audit(json.dumps(raw), span=source.focus, before=[], after=after)
    raw['checks'][0]['original_quote'] = 'This does not occur in the source.'
    with pytest.raises(ValueError, match='summary_invalid'):
        policy.parse_audit(json.dumps(raw), span=source.focus, before=[], after=after)


def test_progress_compression_rejects_status_loss_with_safe_error():
    text, policy = '计划仅在条件满足后执行，目前仍未知。', get('context_preparation')
    source, transport = request(text, limit=3000), SummaryTransport()
    transport.reject = True
    summaries = [{'coverage': ['body'], 'items': [{'kind': 'decision',
        'text': '{"id":"claim-1","event_status":"planned","epistemic_status":"unknown"}',
        'evidence': [{'key': 'body', 'start': 0, 'end': len(text), 'quote': text}]}]}]
    with pytest.raises(ValueError) as error:
        reduce_summaries(source, summaries, spans=[source.focus], complete_stage=transport,
            validate_current=lambda: None, stage_prefix='progress')
    assert str(error.value) == 'context_preparation_progress_semantics_invalid'
    assert any('-audit-' in stage for stage, _ in transport.calls)


def test_progress_singleton_non_shrinking_stops_without_repeated_dispatch():
    source, transport = request('原始条件尚未满足。'), SummaryTransport()
    policy = get('context_preparation')
    proof = {'key': 'body', 'start': 0, 'end': len(source.focus.text), 'quote': source.focus.text}
    rows = [{'coverage': ['body'], 'items': [{'kind': 'decision', 'text': '计划尚未完成。' * 200,
        'evidence': [proof]}]} for _ in (1, 2)]
    single = policy.refine_messages(source, rows[:1])
    double = policy.refine_messages(source, rows)
    from backend.shared.llm.message_metadata import _estimate_input_tokens
    limit = (_estimate_input_tokens(single) + _estimate_input_tokens(double)) // 2
    assert policy.fits(single, limit) and not policy.fits(double, limit)
    source = replace(source, input_limit=limit)
    with pytest.raises(ValueError, match='progress_uncompressible'):
        reduce_summaries(source, rows, spans=[source.focus], complete_stage=transport,
            validate_current=lambda: None, stage_prefix='progress')
    assert len(transport.calls) == 2
