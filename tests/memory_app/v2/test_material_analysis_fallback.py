"""材料第二版真实临时领域链，沿用第一版fixture仅隔离模型传输。"""
import asyncio
from copy import deepcopy
import json
import re
from threading import Lock

import pytest

from backend.memory_app.v2.material_analysis import ANALYSES, read_document_analysis_sources, item_analysis
from backend.memory_app.v2.policies import ACTIVE, override
from backend.memory_app.v2.policies import get
from backend.memory_app.v2.policies.context_preparation import ContextPreparationInput, ContextSpan
from backend.memory_app.v2.material_analysis_fallback import _execute, _hierarchical_review
from backend.shared.llm.message_metadata import _estimate_input_tokens
from tests.memory_app.v2.test_material_analysis import (
    runtime, source_env, confirm, process, PLAN, claim, MaterialTransport,
    test_actual_material_analysis_extract6_and_comment_consumer_full_chain as material_comment_control,
)
from tests.memory_app.v2.test_context_preparation import SummaryTransport


def test_v2_actual_domain_fast_path_confirmation_and_readonly_replay(runtime):
    def frozen_before_dispatch():
        episode = runtime.records.read(ANALYSES, runtime.created['id']).payload
        assert episode['call_limit'] == episode['schedules']['material']['normal_call_limit'] == 3
        assert len(episode['dispatch_stages']) == 2
    runtime.model.on_analysis = frozen_before_dispatch
    with override(material_analysis='@2'):
        result = confirm(runtime)
    assert result['status'] == 'confirmed', result
    assert len(runtime.model.calls) == 3
    row = runtime.records.read('workspace_items', result['id'])
    episode = runtime.records.read(ANALYSES, result['id']).payload
    assert episode['policy_versions'] == {'organize': '@2', 'material_analysis': '@2', 'context_preparation': '@1'}
    assert not episode['schedules']['material']['compressed']
    assert len(item_analysis(runtime.records, row, row.payload['draft'])['claims']) == 3
    sources = read_document_analysis_sources(runtime.records, runtime.documents, 'alpha', result['document_id'])
    assert len(sources['analysis_generation_refs']) == 3
    assert len(runtime.model.calls) == 3
    assert ACTIVE['organize'] == '@1' and ACTIVE['material_analysis'] == '@1' and ACTIVE['compose'] == '@30'


class SmallWindowModel(MaterialTransport):
    def __init__(self):
        self.calls = []
        self.summary = SummaryTransport()
        self.lock = Lock()
        self.requested = False

    def generation_budget_limits(self, **options):
        return {'window': 9000, 'reserve': 1000}

    def complete(self, messages, *, max_tokens, validate_current, **options):
        validate_current()
        values = json.loads(messages[-1]['content'])
        with self.lock:
            self.calls.append((deepcopy(messages), max_tokens))
        if 'protected' in values or 'coverage' in values:
            stage = 'unit-audit-1' if 'protected' in values else 'unit-summary'
            def parse(raw):
                return json.loads(raw)
            value = self.summary(stage, messages, parse)
            if 'summaries' in values:
                for item in value['items']:
                    item['text'] = '保留条件与限制，具体判断仍见原条目。'
                    for proof in item['evidence']:
                        proof['quote'] = proof['quote'][:20]
                        proof['end'] = proof['start'] + len(proof['quote'])
        elif 'blocks' in values:
            value = {'groups': [{'label': '跨段核对', 'summary': '主体与条件保持一致，未知内容仍需核对。',
                'block_keys': [block['key'] for block in values['blocks']],
                'inferences': [], 'contradictions': [], 'unknowns': []}], 'read_requests': []}
        elif 'claims' in values:
            value = {'groups': [{'label': '综合', 'claim_ids': [item['id'] for item in values['claims']],
                'inferences': [], 'contradictions': [], 'unknowns': []}]}
        elif 'focus' in values:
            focus = values['focus']
            operation = json.loads(values['task'])['operation']
            if operation == 'plan':
                value = PLAN
            elif operation == 'analysis':
                with self.lock:
                    need_read = not self.requested
                    self.requested = True
                value = {'claims': [claim('条件与主体状态应保持完整。' * 25, focus['text'].strip())],
                    'read_requests': ([{'key': 'material-body', 'start': 0, 'end': 20,
                        'reason': '核对记录范围'}] if need_read else [])}
            else:
                events = []
                headers = [span for span in values['raw_context'] if span['key'].startswith('record-header-')]
                for match in re.finditer(r'Session date: [0-9-]+', focus['text']):
                    headers.append({'start': focus['start'] + match.start(), 'end': focus['start'] + match.end(), 'text': match.group()})
                for match in re.finditer(r'Lin completed item \d+ yesterday\.', focus['text']):
                    nearest = next((header for header in sorted(headers, key=lambda row: row['end'], reverse=True)
                        if header['end'] <= focus['start'] + match.start()), None)
                    events.append({'actors': ['Lin'], 'event_text': match.group(), 'quote': match.group(),
                        'expression': 'yesterday', 'anchor_quote': nearest['text'] if nearest else None,
                        'anchor_role': 'record' if nearest else 'unknown', 'event_status': 'asserted'})
                value = {'events': events, 'read_requests': []}
        elif 'plan' in values:
            value = {'claims': [claim('材料中的条件约束。', values['source'].strip())]}
        elif 'events' in messages[0]['content']:
            value = {'events': []}
        else:
            value = PLAN
        validate_current()
        return json.dumps(value, ensure_ascii=False), {'model': 'fixture', 'usage': {'input_tokens': 10, 'output_tokens': 20}}


def long_source(count=36):
    return 'Session date: 2024-03-01\n\n' + '\n\n'.join(
        ('Session date: 2024-03-04\n\n' if index == count // 2 else '') +
        '第' + str(index) + '项。' + '只有明确条件满足才执行；否则保留未知并回查原文，不能将计划写成已经完成。' * 3 +
        ' Lin completed item ' + str(index) + ' yesterday.' for index in range(count))


def long_runtime(runtime):
    source = long_source()
    runtime.created = asyncio.run(runtime.domains.intake.add_text({'project_id': 'alpha', 'text': source,
        'title': 'Independent small-window fixture', 'preserve_text': True}))
    runtime.model.__class__ = SmallWindowModel
    SmallWindowModel.__init__(runtime.model)
    return source


def test_singleton_review_preprocessing_preserves_original_claims():
    policy, context = get('material_analysis', version='@2'), get('context_preparation')
    claims = [{**claim('计划仍是计划。' * 100, '原始依据。' * 100, status='planned', kind='plan'),
        'id': 'claim-' + str(index)} for index in (1, 2)]
    for row in claims:
        row['evidence'][0].update(start=0, end=len(row['evidence'][0]['quote']))
    original = deepcopy(claims)
    blocks = [{'key': 'block-' + str(index), 'summary': json.dumps(row, ensure_ascii=False, sort_keys=True)}
        for index, row in enumerate(claims, 1)]
    def size(values):
        return _estimate_input_tokens(policy.review_messages(values, title='材料', plan=PLAN, structure=(),
            source_catalog=[{'key': 'body', 'start': 0, 'end': 10}]))
    limit = (size(blocks[:1]) + size(blocks)) // 2
    assert size(blocks[:1]) <= limit < size(blocks)
    text = claims[0]['evidence'][0]['quote']
    span = ContextSpan('body', {'type': 'original_item'}, 'source_v1', 0, len(text), text)
    request = ContextPreparationInput('材料', '材料', [], span, (), (span,), limit, 4000)
    stages = []
    audit = SummaryTransport()
    def stage(key, messages, parser):
        assert context.fits(messages, limit)
        values = json.loads(messages[-1]['content'])
        stages.append(key)
        if 'protected' in values:
            return audit(key, messages, parser)
        return parser(json.dumps({'groups': [{'label': '计划约束', 'summary': '仍处于计划阶段，不能当成已完成事实。',
            'block_keys': [row['key'] for row in values['blocks']], 'inferences': [], 'contradictions': [], 'unknowns': []}],
            'read_requests': []}, ensure_ascii=False))
    result = _hierarchical_review(claims, title='材料', plan=PLAN, structure=(), policy=policy,
        context=context, input_limit=limit, stage=stage, request=request,
        read_original=lambda ref: span.text, validate=lambda: None)
    assert len([key for key in stages if '-audit-' not in key]) == 3 and claims == original
    assert any('-audit-' in key for key in stages)
    assert sorted(identity for group in result['groups'] for identity in group['claim_ids']) == ['claim-1', 'claim-2']


@pytest.mark.parametrize('claim_count', [0, 1])
def test_review_zero_or_one_claim_still_dispatches_structure_review_and_audit(claim_count):
    source = '只有条件满足才执行；状态仍未知。'
    span = ContextSpan('body', {'type': 'original_item'}, 'source_v1', 0, len(source), source)
    request = ContextPreparationInput('材料', '检查条件与未知', [], span, (), (span,), 5000, 4000)
    rows = [{**claim('条件满足才执行，仍未知。', source, status='unknown', kind='unknown'), 'id': 'claim-1'}][:claim_count]
    for row in rows:
        row['evidence'][0].update(start=0, end=len(source))
    structure = ({'coverage': ['body'], 'items': [{'kind': 'condition', 'text': source,
        'evidence': [{'key': 'body', 'start': 0, 'end': len(source), 'quote': source}]}]},)
    model, stages = SmallWindowModel(), []
    def stage(key, messages, parser):
        stages.append(key)
        raw, _ = model.complete(messages, max_tokens=4000, validate_current=lambda: None)
        return parser(raw)
    result = _hierarchical_review(rows, title='材料', plan=PLAN, structure=structure,
        policy=get('material_analysis', version='@2'), context=get('context_preparation'), input_limit=5000,
        stage=stage, request=request, read_original=lambda ref: source, validate=lambda: None)
    assert 'material-review-1-1' in stages and any('-audit-' in key for key in stages)
    assert sum(len(group['claim_ids']) for group in result['groups']) == claim_count


def test_review_summary_status_loss_rejected_by_original_item_audit():
    source = '只有条件满足才执行；计划尚未完成。'
    span = ContextSpan('body', {'type': 'original_item'}, 'source_v1', 0, len(source), source)
    request = ContextPreparationInput('材料', '检查计划状态', [], span, (), (span,), 5000, 4000)
    row = {**claim('计划尚未完成。', source, status='planned', kind='plan'), 'id': 'claim-1'}
    row['evidence'][0].update(start=0, end=len(source))
    model = SmallWindowModel()
    model.summary.reject = True
    def stage(key, messages, parser):
        raw, _ = model.complete(messages, max_tokens=4000, validate_current=lambda: None)
        return parser(raw)
    with pytest.raises(ValueError, match='context_preparation_progress_semantics_invalid'):
        _hierarchical_review([row], title='材料', plan=PLAN, structure=(),
            policy=get('material_analysis', version='@2'), context=get('context_preparation'), input_limit=5000,
            stage=stage, request=request, read_original=lambda ref: source, validate=lambda: None)
    protected = [json.loads(messages[-1]['content'])['protected'] for messages, _ in model.calls
        if 'protected' in json.loads(messages[-1]['content'])]
    assert any('planned' in item.get('text', '') for group in protected for item in group)


def test_small_window_preflight_same_builder_zero_external_api():
    source, model = long_source(), SmallWindowModel()
    episode = {'item_id': 'preflight', 'project_id': 'alpha', 'owner_revision': 1,
        'original_title': 'Independent small-window fixture', 'body_range': {'start': 0, 'end': len(source)},
        'input_limit': 4500, 'output_limit': 4500}
    stages = []
    def complete(key, messages, parser):
        stages.append(key)
        raw, _meta = model.complete(messages, max_tokens=4500, validate_current=lambda: None)
        return parser(raw)
    result = _execute(episode, source_text=source, complete=complete, freeze=lambda key, value: None,
        validate=lambda: None, read_original=lambda ref: source, workers=1)
    assert result['schedules']['material']['compressed']
    assert any('-map-' in key for key in stages) and any('-reduce-' in key for key in stages)
    assert any(key.startswith('material-progress-') for key in stages)
    assert any(key.endswith('-recheck') for key in stages)
    assert any(key.startswith('material-review-') for key in stages)
    assert {event['computed'] for event in result['analysis']['temporal']} == {'2024-02-29', '2024-03-03'}
    print('SOURCE-ONLY-PREFLIGHT', {'characters': len(source), 'calls': len(stages),
        'focuses': len(result['schedules']['material']['focuses']),
        'stages': {prefix: sum(key.startswith(prefix) for key in stages) for prefix in (
            'material-context', 'material-plan', 'material-analysis', 'material-temporal', 'material-progress', 'material-review')}})


class ReusedStructureModel(SmallWindowModel):
    def __init__(self, *, compressible):
        super().__init__()
        self.compressible = compressible

    def complete(self, messages, *, max_tokens, validate_current, **options):
        values = json.loads(messages[-1]['content'])
        if 'coverage' not in values:
            return super().complete(messages, max_tokens=max_tokens, validate_current=validate_current, **options)
        validate_current()
        self.calls.append((deepcopy(messages), max_tokens))
        if 'source' in values:
            source = values['source']
            items = [{'kind': 'condition', 'text': '条件未知。' * 300,
                'evidence': [{'key': source['key'], 'start': source['start'],
                    'end': source['start'] + 20, 'quote': source['text'][:20]}]}]
        else:
            items = [deepcopy(item) for summary in values['summaries'] for item in summary['items']]
            if self.compressible and '再压缩一次表达' in messages[0]['content']:
                for item in items:
                    item['text'] = '须满足条件，未知待核查。'
                    for proof in item['evidence']:
                        proof['quote'] = proof['quote'][:20]
                        proof['end'] = proof['start'] + len(proof['quote'])
        validate_current()
        return json.dumps({'coverage': values['coverage'], 'items': items}, ensure_ascii=False), {
            'model': 'fixture', 'usage': {'input_tokens': 10, 'output_tokens': 20}}


@pytest.mark.parametrize('compressible', [True, False])
def test_reused_plan_structure_refines_for_analysis_budget_and_replays(compressible):
    source, model = long_source(), ReusedStructureModel(compressible=compressible)
    episode = {'item_id': 'reuse', 'project_id': 'alpha', 'owner_revision': 1,
        'original_title': 'Independent small-window fixture', 'body_range': {'start': 0, 'end': len(source)},
        'input_limit': 4500, 'output_limit': 4500}
    saved, schedules, calls = {}, {}, []
    def complete(key, messages, parser):
        calls.append(key)
        raw, _ = model.complete(messages, max_tokens=4500, validate_current=lambda: None)
        saved[key] = (deepcopy(messages), raw)
        return parser(raw)
    def run(adapter, freeze):
        return _execute(episode, source_text=source, complete=adapter, freeze=freeze,
            validate=lambda: None, read_original=lambda ref: source, workers=1)
    if not compressible:
        with pytest.raises(ValueError, match='progress_uncompressible'):
            run(complete, lambda key, value: schedules.update({key: deepcopy(value)}))
        assert len([key for key in calls if key.startswith('material-context-reuse-refine')]) == 2
        assert not any(key.startswith('material-analysis-') for key in calls)
        return
    result = run(complete, lambda key, value: schedules.update({key: deepcopy(value)}))
    assert len([key for key in calls if key.startswith('material-context-reuse-refine')]) == 5
    assert len(result['schedules']['material']['focuses']) == 4
    assert {event['computed'] for event in result['analysis']['temporal']} == {'2024-02-29', '2024-03-03'}
    assert all(item['text'] == '须满足条件，未知待核查。' for summary in result['context_structure'] for item in summary['items'])
    assert any(len(item['text']) > 1000 for summary in result['context_source_structure'] for item in summary['items'])
    count = len(model.calls)
    def replay(key, messages, parser):
        expected, raw = saved[key]
        assert messages == expected
        return parser(raw)
    def frozen(key, schedule):
        assert schedules[key] == schedule
    assert run(replay, frozen) == result and len(model.calls) == count


def test_v2_small_window_actual_turns_original_lookup_dates_and_replay(runtime):
    source = long_runtime(runtime)
    with override(material_analysis='@2'):
        result = confirm(runtime)
    assert result['status'] == 'confirmed', result
    payload = runtime.records.read(ANALYSES, result['id']).payload
    assert payload['schedules']['material']['compressed']
    assert any('-audit-' in key for key in payload['stage_names'])
    assert any(key.endswith('-recheck') for key in payload['stage_names'])
    assert any(key.startswith('material-progress-') for key in payload['stage_names'])
    assert any(key.startswith('material-review-') for key in payload['stage_names'])
    focuses = payload['schedules']['material']['focuses']
    assert ''.join(part['text'] for part in focuses) == source
    assert {event['computed'] for event in payload['analysis']['temporal']} == {'2024-02-29', '2024-03-03'}
    count = len(runtime.model.calls)
    assert payload['call_limit'] == count
    row = runtime.records.read('workspace_items', result['id'])
    assert item_analysis(runtime.records, row, row.payload['draft']) == payload['analysis']
    assert read_document_analysis_sources(runtime.records, runtime.documents, 'alpha', result['document_id'])['body_sources'][0]['text'] == source
    assert len(runtime.model.calls) == count
    assert runtime.records.list('recognitions') == ()


def test_v2_small_window_rejects_semantic_summary_loss_before_analysis(runtime):
    long_runtime(runtime)
    runtime.model.summary.reject = True
    with override(material_analysis='@2'):
        result = confirm(runtime)
    assert result['status'] == 'failed', result
    assert not any('focus' in json.loads(messages[-1]['content']) for messages, _ in runtime.model.calls)


def test_v2_manual_draft_edit_preserves_generation_and_replay(runtime):
    with override(material_analysis='@2'):
        ready = process(runtime)
        original = deepcopy(runtime.records.read(ANALYSES, ready['id']).payload)
        saved = asyncio.run(runtime.domains.review.save_draft(ready['id'], {
            'project_id': 'alpha', 'expected_revision': ready['revision'], **ready['draft'],
            'title': '人工核对标题', 'summary': '人工补充摘要'}))
        preview = runtime.domains.review.image_markdown(saved['id'], 'alpha', saved['draft'])
        assert '人工核对标题' in preview and '人工补充摘要' in preview and 'claim-3' in preview
        result = asyncio.run(runtime.domains.review.confirm(saved['id'], {
            'project_id': 'alpha', 'expected_revision': saved['revision']}))
    assert result['status'] == 'confirmed'
    payload = runtime.records.read(ANALYSES, result['id']).payload
    assert payload['analysis'] == original['analysis'] and payload['generated_draft'] == original['draft']
    assert payload['draft']['summary'] == '人工补充摘要'
    assert len(read_document_analysis_sources(runtime.records, runtime.documents, 'alpha', result['document_id'])['analysis_generation_refs']) == 3
    assert len(runtime.model.calls) == 3


@pytest.mark.parametrize('manifest_change', [None, 'source_revoked'])
def test_v2_extract6_actual_owner_comment_path_and_revocation(source_env, manifest_change):
    with override(material_analysis='@2'):
        material_comment_control(source_env, manifest_change)
