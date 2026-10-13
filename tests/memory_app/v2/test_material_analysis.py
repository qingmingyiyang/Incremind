"""真实临时入库与冻结确认；模型仅替换传输，不替换领域服务。"""
import asyncio
from copy import deepcopy
import json
from pathlib import Path
from types import SimpleNamespace
from threading import Barrier, Event, Lock

import pytest
from fastapi import FastAPI, HTTPException

from backend.memory_app.workspace import install_workspace_routes
from backend.memory_app.v2.auto_confirm import process_and_confirm
from backend.memory_app.v2.material_analysis import (
    ANALYSES, PUBLICATIONS, item_analysis, read_document_analysis, render_analysis, selected_flow,
    read_document_analysis_sources, _limits,
)
from backend.memory_app.v2.organize_turns import OrganizeTurns
from backend.memory_app.v2.policies import override
from backend.memory_app.v2.policies.material_analysis import (
    parse_claims, parse_plan, parse_temporal, parse_synthesis, v1,
)
from backend.memory_app.v2.policies.pipelines import versions_for_turn
from backend.recognition import RecognitionService
from core.document_engine import SQLiteDocumentRepository
from core.storage_provider import SQLiteStructuredRecordStore
from tests.memory_app.governed_model_fixture import GovernedModel
from tests.memory_app.v2.test_source_sections import source_env


SOURCE = ('Session date: 2023-06-27\n'
    'Mira plans to build a telescope next month.\n'
    'Her brother Sol visited the observatory last week.\n'
    'The launch date is unknown.')
PLAN = {'title': 'Observatory notes', 'summary': 'Plans, another person’s visit and an unknown date.',
    'angles': [{'id': 'agency', 'label': '主体与状态', 'focus': '区分主体和计划'},
        {'id': 'constraints', 'label': '限制', 'focus': '未知与时间'}],
    'temporal': {'needed': True, 'reason': '材料含相对日期'}}


def claim(text, quote, *, status='asserted', kind='reported_fact', actor='Mira'):
    return {'angle_ids': ['agency', 'constraints'], 'text': text, 'epistemic_status': kind,
        'event_status': status, 'actors': [actor], 'entities': ['observatory'], 'evidence': [{'quote': quote}]}


class MaterialTransport(GovernedModel):
    def generation_budget_limits(self, *, expected_revision, max_tokens=None):
        assert expected_revision == self.public()['generation']['revision']
        return {'window': 200000, 'reserve': 7000}


class Model(MaterialTransport):
    def __init__(self):
        self.calls = []
        self.on_analysis = None

    def complete(self, messages, *, max_tokens, validate_current=None, **options):
        validate_current()
        values = json.loads(messages[-1]['content'])
        self.calls.append((deepcopy(messages), max_tokens))
        if 'plan' in values:
            if self.on_analysis:
                self.on_analysis()
            value = {'claims': [
                claim('Mira plans to build a telescope; completion is not established.',
                    'Mira plans to build a telescope next month.', status='planned', kind='plan'),
                claim('Sol visited the observatory.', 'Her brother Sol visited the observatory last week.', actor='Sol'),
                claim('The launch date is unknown.', 'The launch date is unknown.', status='unknown', kind='unknown'),
            ]}
        elif 'events' in messages[0]['content']:
            value = {'events': [{'actors': ['Sol'], 'event_text': 'Sol visited the observatory.',
                'quote': 'Her brother Sol visited the observatory last week.',
                'expression': 'last week', 'anchor_quote': 'Session date: 2023-06-27',
                'anchor_role': 'record', 'event_status': 'asserted'}]}
        else:
            value = PLAN
        return json.dumps(value, ensure_ascii=False), {'model': 'fake', 'usage': {'input_tokens': 20, 'output_tokens': 30}}


@pytest.fixture
def runtime(tmp_path, monkeypatch):
    monkeypatch.setenv('CHRIPTMAS_APP_ROOT', str(tmp_path))
    records = SQLiteStructuredRecordStore(tmp_path / '.rebuild-data' / 'records.sqlite3')
    documents = SQLiteDocumentRepository(records, namespace_id='default')
    model = Model()
    domains = install_workspace_routes(FastAPI(), runtime_root=tmp_path, records=records,
        models=model, documents=documents, service=RecognitionService(records))
    created = asyncio.run(domains.intake.add_text({'project_id': 'alpha', 'text': SOURCE,
        'title': 'Original field notes', 'preserve_text': True}))
    return SimpleNamespace(records=records, documents=documents, model=model, domains=domains,
        created=created, root=tmp_path)


def process(runtime):
    with override(organize='@2'):
        return asyncio.run(runtime.domains.intake.process(runtime.created['id'], {'project_id': 'alpha'}))


def confirm(runtime):
    with override(organize='@2'):
        return asyncio.run(process_and_confirm(runtime.domains, runtime.created['id'], 'alpha'))


def test_material_plan_analysis_time_confirm_retrievable_l1_without_published_l3(runtime):
    def frozen_before_dispatch():
        episode = runtime.records.read(ANALYSES, runtime.created['id']).payload
        assert episode['plan'] == PLAN and episode['call_limit'] == 3
        assert len(episode['analysis_stages']) == len(episode['temporal_stages']) == 1
    runtime.model.on_analysis = frozen_before_dispatch
    result = confirm(runtime)
    assert result['status'] == 'confirmed', result
    assert len(runtime.model.calls) == 3
    assert runtime.records.read('workspace_items', result['id']).payload['model_usage'] == {
        'input_tokens': 60, 'output_tokens': 90}
    assert all(tokens == 7000 for _, tokens in runtime.model.calls)
    body = runtime.documents.markdown(result['document_id'])
    assert '计划中' in body and 'Sol visited' in body and 'launch date is unknown' in body
    assert body.count('原文依据："Mira plans') == 1
    assert '程序推算' in body and '日历周' in body and '连续七天' in body
    assert '不能确定具体哪一天' in body
    doc = runtime.documents.read(result['document_id'])
    assert any(ref.get('quote') == 'The launch date is unknown.' for ref in doc['source_refs'])
    index = runtime.records.read('document_retrieval_index', result['document_id']).payload
    assert any('telescope' in chunk['text'] for span in index['spans'] for chunk in span['chunks'])
    recall = runtime.domains.query.collect_candidates('alpha', 'telescope')['candidates']
    assert any(row['layer'] == 'L1' and row['entry']['id'] == result['document_id'] for row in recall)
    published = read_document_analysis(runtime.records, runtime.documents, 'alpha', result['document_id'])
    assert published['analysis']['claims'][0]['event_status'] == 'planned'
    assert published['analysis']['claims'][1]['actors'] == ['Sol']
    assert published['original_material_refs'][0]['type'] == 'original_item'
    assert runtime.records.list('recognitions') == ()
    steps = runtime.records.list('workspace_organize_steps')
    turns = OrganizeTurns(root=runtime.root, records=runtime.records, models=runtime.model,
        item_id=result['id'], project_id='alpha', source=SOURCE, validate_current=lambda: None).store
    assert len(steps) == 3
    for step in steps:
        request = turns.get_request(step.payload['turn_id'])
        assert request['policy_versions']['material_analysis'] == '@1'
        assert turns.events_after(step.payload['turn_id'])[-1]['type'] == 'turn.completed'


def test_pending_recovery_uses_publication_even_if_analysis_deleted(runtime, monkeypatch):
    original = type(runtime.domains.confirmations.source_store).write
    def fail(*args, **kwargs):
        raise OSError('synthetic interruption')
    monkeypatch.setattr(type(runtime.domains.confirmations.source_store), 'write', fail)
    failed = confirm(runtime)
    assert failed['status'] == 'confirming'
    publication = runtime.records.read(PUBLICATIONS, 'confirm-' + failed['id'])
    with runtime.records.begin() as tx:
        analysis = tx.read(ANALYSES, failed['id'])
        tx.delete(ANALYSES, failed['id'], expected_revision=analysis.revision)
        tx.commit()
    monkeypatch.setattr(type(runtime.domains.confirmations.source_store), 'write', original)
    resumed = confirm(runtime)
    assert resumed['status'] == 'confirmed'
    assert runtime.documents.markdown(resumed['document_id']) == publication.payload['markdown']
    assert len(runtime.model.calls) == 3


def test_insufficient_model_capacity_is_explicit_and_dispatches_nothing(runtime):
    runtime.model.generation_budget_limits = lambda **options: {'window': 600, 'reserve': 500}
    result = process(runtime)
    assert result['status'] == 'failed' and result['error'] == 'material_analysis_budget_insufficient'
    assert runtime.model.calls == [] and runtime.documents.list() == ()
    assert runtime.records.read(ANALYSES, result['id']).payload['state'] == 'processing'


def test_unique_full_quote_disambiguates_repeated_words():
    source = 'Mira said Hello. Sol said Hello.'
    raw = json.dumps({'claims': [claim('Sol greeted someone.', 'Sol said Hello.', actor='Sol')]})
    parsed = parse_claims(raw, piece=source, offset=10, plan=PLAN)
    evidence = parsed[0]['evidence'][0]
    assert evidence['start'] == 27 and evidence['end'] == 42


@pytest.mark.parametrize('change', ['claim', 'stages', 'source', 'draft'])
def test_preview_confirmation_rejects_changed_analysis_source_or_draft(runtime, change):
    process(runtime)
    item_id = runtime.created['id']
    collection = ANALYSES if change in {'claim', 'stages'} else 'workspace_items'
    row = runtime.records.read(collection, item_id)
    payload = deepcopy(row.payload)
    if change == 'claim':
        payload['analysis']['claims'][0]['text'] = 'Mira completed construction.'
    elif change == 'stages':
        payload['analysis_stages'] = []
        payload['analysis']['claims'] = []
    elif change == 'source':
        payload['source_text'] += '\nChanged.'
    else:
        payload['draft']['summary'] = 'Changed.'
    with runtime.records.begin() as tx:
        tx.put(collection, item_id, payload, expected_revision=row.revision)
        tx.commit()
    row = runtime.records.read('workspace_items', item_id)
    with pytest.raises((ValueError, HTTPException)):
        asyncio.run(runtime.domains.review.confirm(item_id, {'project_id': 'alpha', 'expected_revision': row.revision}))
    assert runtime.documents.list() == ()


def test_pipeline_versions_only_add_material_for_new_organize_and_propose():
    baseline = versions_for_turn('memory.organize')
    assert 'material_analysis' not in baseline
    with override(organize='@2'):
        assert versions_for_turn('memory.organize')['material_analysis'] == '@1'
        assert versions_for_turn('memory.propose_insights')['material_analysis'] == '@1'
        assert 'material_analysis' not in versions_for_turn('memory.place')


def test_registered_material_key_is_accepted_by_both_contracts_only():
    from core.ai_kernel import AIKernelContractError, validate_turn_request
    from jsonschema import Draft202012Validator
    root = Path(__file__).resolve().parents[3] / 'core-contracts' / 'ai'
    request = json.loads((root / 'fixtures' / 'turn-request' / 'valid-project-answer.json').read_text(encoding='utf-8'))
    validator = Draft202012Validator(json.loads((root / 'turn-request.schema.json').read_text(encoding='utf-8')))
    assert validate_turn_request(request) == request and validator.is_valid(request)
    request['policy_versions'] = {'organize': '@2', 'material_analysis': '@1'}
    assert validate_turn_request(request) == request and validator.is_valid(request)
    request['policy_versions']['unregistered_analysis'] = '@1'
    assert not validator.is_valid(request)
    with pytest.raises(AIKernelContractError, match='policy versions are invalid'):
        validate_turn_request(request)


@pytest.mark.parametrize('change', ['missing', 'revoked'])
def test_pending_material_and_comments_still_check_revoked_comment_source(source_env, change):
    from backend.memory_app.v2.source_sections import SECTIONS
    from tests.memory_app.v2.test_comment_document_section import suspend_confirmation_with_real_json_directory_failure
    env = source_env
    class CommentMaterialModel(MaterialTransport):
        def complete(self, messages, *, max_tokens, validate_current=None, **options):
            validate_current()
            values = json.loads(messages[-1]['content'])
            if 'plan' in values:
                quote = values['source'].splitlines()[0]
                value = {'claims': [claim(quote, quote, actor='主体未确定')]}
            else:
                value = {**PLAN, 'temporal': {'needed': False, 'reason': '不需要时间挖掘'}}
            return json.dumps(value), {}
    env.domains.intake.models = CommentMaterialModel()
    with override(organize='@2'):
        created = asyncio.run(env.domains.intake.add_link({'project_id': 'alpha', 'url': env.item['url']}))
        ready = asyncio.run(env.domains.intake.process(created['id'], {'project_id': 'alpha'}))
    assert ready['status'] == 'ready', ready.get('error')
    assert len([step for step in env.records.list('workspace_organize_steps')
        if step.object_id.startswith('organize-' + created['id'] + '-')]) == 2
    row, operation = suspend_confirmation_with_real_json_directory_failure(env, created['id'], ready['revision'])
    assert env.records.read(PUBLICATIONS, operation.object_id) is not None
    assert '## 材料分析' in operation.payload['markdown'] and '## 评论区' in operation.payload['markdown']
    with env.records.begin() as tx:
        proof = tx.read(SECTIONS, created['id'])
        if change == 'missing':
            tx.delete(SECTIONS, created['id'], expected_revision=proof.revision)
        else:
            tx.put(SECTIONS, created['id'], {**proof.payload, 'state': 'invalidated'}, expected_revision=proof.revision)
        tx.commit()
    with pytest.raises(HTTPException) as error:
        asyncio.run(env.domains.review.confirm(created['id'], {'project_id': 'alpha',
            'expected_revision': row.payload['reviewed_revision']}))
    assert error.value.status_code == 409 and env.documents.list() == ()


def test_old_episode_does_not_switch_to_new_material_stages(runtime):
    from tests.memory_app.test_workspace import Model as OldModel
    organize = OrganizeTurns(root=runtime.root, records=runtime.records, models=OldModel(),
        item_id=runtime.created['id'], project_id='alpha', source=SOURCE, validate_current=lambda: None)
    organize.complete([{'role': 'user', 'content': SOURCE}], max_tokens=700, validate_current=lambda: None)
    with override(organize='@2'):
        assert selected_flow(organize) is False


@pytest.mark.parametrize('operation', ['plan', 'analysis', 'temporal'])
def test_policy_contract_does_not_accept_future_question_or_gold(operation):
    values = {'original_title': 'Material', 'source': 'Raw material'}
    if operation == 'analysis':
        values['plan'] = PLAN
    with pytest.raises(ValueError, match='material_analysis_input_invalid'):
        v1(operation, **values, question='Future question', gold='Answer')


@pytest.mark.parametrize('source,quote,expression,anchor,role,known', [
    ('Session date: 2023-06-27\nI visited yesterday.', 'I visited yesterday.', 'yesterday', 'Session date: 2023-06-27', 'record', True),
    ('I visited yesterday.', 'I visited yesterday.', 'yesterday', None, 'unknown', False),
    ('Session date: 2023-06-27\nHe said, "I visited yesterday."', 'He said, "I visited yesterday."', 'yesterday', 'Session date: 2023-06-27', 'record', False),
    ('Session date: 2023-06-27\nThe show ended and 3 days later we left.', 'The show ended and 3 days later we left.', '3 days later', 'Session date: 2023-06-27', 'record', False),
    ('On 2023-06-20 we talked. Yesterday I left.', 'On 2023-06-20 we talked. Yesterday I left.', 'Yesterday', '2023-06-20', 'local_explicit', False),
    ('于2023-06-20见面。昨天离开。', '于2023-06-20见面。昨天离开。', '昨天', '2023-06-20', 'local_explicit', False),
    ('The deadline is 2023-06-27. I visited yesterday.', 'I visited yesterday.', 'yesterday', '2023-06-27', 'record', False),
])
def test_time_reference_is_local_and_not_ingestion_clock(source, quote, expression, anchor, role, known):
    raw = json.dumps({'events': [{'actors': [], 'event_text': quote,
        'quote': quote, 'expression': expression, 'anchor_quote': anchor,
        'anchor_role': role, 'event_status': 'asserted'}]})
    event = parse_temporal(raw, piece=source, offset=0, original_title='Material')[0]
    assert (event['event_time']['status'] == 'known') is known
    if known:
        assert event['event_time']['options'][0]['start'] == '2023-06-26'


def test_week_precision_retains_both_conventions_in_real_l1_render():
    raw = json.dumps({'events': [{'actors': ['Sol'], 'event_text': 'Sol visited the observatory.',
        'quote': SOURCE.splitlines()[2], 'expression': 'last week',
        'anchor_quote': SOURCE.splitlines()[0], 'anchor_role': 'record', 'event_status': 'asserted'}]})
    events = parse_temporal(raw, piece=SOURCE, offset=0, original_title='Material')
    assert events[0]['event_time']['precision'] == 'week'
    assert len(events[0]['event_time']['options']) == 2
    text = render_analysis({'plan': PLAN, 'claims': [], 'temporal': events})
    assert '程序推算' in text and '日历周' in text and '连续七天' in text
    assert '不能确定具体哪一天' in text


def test_unique_quote_required_instead_of_picking_first_duplicate():
    raw = json.dumps({'claims': [claim('Uncertain speaker.', 'Hello.')]})
    with pytest.raises(ValueError, match='material_analysis_quote_invalid'):
        parse_claims(raw, piece='Hello. Hello.', offset=0, plan=PLAN)


def test_no_reusable_claims_is_valid_and_continuous_segments_obey_output_budget():
    assert parse_claims('{"claims":[]}', piece='one-time note', offset=0, plan=parse_plan(json.dumps(PLAN))) == []
    source = ('长材料需要完整连续覆盖。' * 2000)
    parts = v1.pieces(source, lambda text, offset: v1.focused('analysis', original_title='Long',
        source=text, full_text=source, body_start=0, offset=offset, plan=PLAN),
        input_limit=193000, output_limit=7000, with_offsets=True)
    assert len(parts) > 1 and ''.join(piece for _, piece in parts) == source
    assert [offset for offset, _ in parts] == [sum(len(piece) for _, piece in parts[:index]) for index in range(len(parts))]
    with pytest.raises(ValueError, match='material_analysis_budget_insufficient'):
        v1.pieces('x', lambda text: v1('plan', original_title='Small', source=text), input_limit=1)


class LongMaterialModel(MaterialTransport):
    def __init__(self, *, fail_first=False, parallel=True):
        self.calls, self.finished, self.attempts = [], [], []
        self.lock, self.barrier, self.second_finished = Lock(), Barrier(2), Event()
        self.active = self.peak = 0
        self.fail_first, self.parallel = fail_first, parallel

    def public(self):
        value = super().public()
        value['generation'].update(base_url='https://model.example.test/v1', allow_remote=True)
        return value

    def complete(self, messages, *, max_tokens, validate_current=None, **options):
        validate_current()
        value = json.loads(messages[-1]['content'])
        if 'claims' in value:
            operation, identity = 'synthesis', 'all'
            ids = [item['id'] for item in value['claims']]
            output = {'groups': [{'label': '完整综合', 'claim_ids': ids,
                'inferences': [{'text': '分别保留计划、已知与未知状态。',
                    'epistemic_status': 'inference', 'claim_ids': ids}],
                'contradictions': [], 'unknowns': []}]}
        elif 'plan' in value:
            operation, identity = 'analysis', value['source'].split('.')[0]
            with self.lock:
                self.attempts.append((operation, identity))
                self.active += 1
                self.peak = max(self.peak, self.active)
            try:
                if self.parallel and identity in {'Section 1', 'Section 2'}:
                    self.barrier.wait(timeout=10)
                    if identity == 'Section 1':
                        assert self.second_finished.wait(timeout=10)
                    else:
                        self.second_finished.set()
                if identity == 'Section 1' and self.fail_first:
                    self.fail_first = False
                    raise ValueError('synthetic material failure')
                status, kind = ('planned', 'plan') if identity == 'Section 1' else (
                    ('unknown', 'unknown') if identity == 'Section 3' else ('asserted', 'reported_fact'))
                output = {'claims': [claim(identity + ' retains its state.', identity + '.',
                    status=status, kind=kind)]}
                with self.lock:
                    self.finished.append(identity)
            finally:
                with self.lock:
                    self.active -= 1
        else:
            operation, identity = 'plan', 'all'
            output = {**PLAN, 'temporal': {'needed': False, 'reason': '概念材料不需要时间分析'}}
        with self.lock:
            self.calls.append((operation, identity, deepcopy(value)))
        return json.dumps(output, ensure_ascii=False), {'model': 'fake', 'usage': {'input_tokens': 20, 'output_tokens': 30}}


def long_runtime(runtime, model):
    source = '\n\n'.join('Section ' + str(index) + '. ' + 'detail ' * 750 for index in range(1, 4)).strip()
    runtime.created = asyncio.run(runtime.domains.intake.add_text({'project_id': 'alpha',
        'text': source, 'title': 'Long concepts', 'preserve_text': True}))
    runtime.model = runtime.domains.intake.models = model
    return source


def test_long_material_parallel_workers_and_total_synthesis_preserve_every_claim(runtime):
    model = LongMaterialModel()
    source = long_runtime(runtime, model)
    result = confirm(runtime)
    assert result['status'] == 'confirmed', result
    episode = runtime.records.read(ANALYSES, result['id']).payload
    assert episode['call_limit'] == 5 and episode['parallelism'] == 2
    assert model.peak == 2 and model.finished.index('Section 2') < model.finished.index('Section 1')
    analysis_inputs = [value['source'] for operation, _, value in model.calls if operation == 'analysis']
    for operation, _, values in model.calls:
        if operation == 'analysis':
            assert values['full_text'] == source
            start, end = values['focus_range']['start'], values['focus_range']['end']
            assert values['body_start'] == 0 and source[start:end] == values['source']
    assert ''.join(sorted(analysis_inputs, key=lambda value: value.split('.')[0])) == source
    published = read_document_analysis(runtime.records, runtime.documents, 'alpha', result['document_id'])
    claims = published['analysis']['claims']
    assert [item['id'] for item in claims] == ['claim-1', 'claim-2', 'claim-3']
    assert [item['event_status'] for item in claims] == ['planned', 'asserted', 'unknown']
    assert published['analysis']['synthesis']['groups'][0]['claim_ids'] == ['claim-1', 'claim-2', 'claim-3']
    markdown = runtime.documents.markdown(result['document_id'])
    assert '跨段综合复核' in markdown and '推断：分别保留计划' in markdown
    assert '计划中' in markdown and '未知' in markdown
    assert runtime.records.list('recognitions') == ()


def test_parallel_failure_settles_other_workers_and_resume_reuses_successful_steps(runtime):
    model = LongMaterialModel(fail_first=True)
    long_runtime(runtime, model)
    result = process(runtime)
    assert result['status'] == 'failed' and model.active == 0
    steps = [row for row in runtime.records.list('workspace_organize_steps')
        if row.object_id.startswith('organize-' + result['id'] + '-')]
    assert len(steps) == 4
    assert len([row for row in steps if 'output' in row.payload]) == 3
    assert runtime.records.read(ANALYSES, result['id']).payload['state'] == 'processing'
    assert runtime.documents.list() == ()
    # 已失败的外部调用只在显式重试中再发；成功分段与计划直接读取原检查点。
    model.parallel = False
    resumed = confirm(runtime)
    assert resumed['status'] == 'confirmed', resumed
    assert [identity for operation, identity, _ in model.calls if operation == 'analysis'] == [
        'Section 2', 'Section 3', 'Section 1']
    assert len([item for item in model.calls if item[0] == 'plan']) == 1
    assert len([item for item in model.calls if item[0] == 'synthesis']) == 1
    assert model.attempts.count(('analysis', 'Section 1')) == 2
    assert model.attempts.count(('analysis', 'Section 2')) == model.attempts.count(('analysis', 'Section 3')) == 1
    assert read_document_analysis_sources(runtime.records, runtime.documents, 'alpha', resumed['document_id'])['body_sources'][0]['text']


@pytest.mark.parametrize('main_remote,aux_remote,expected', [(True, False, 1), (False, True, 2)])
def test_parallelism_uses_same_actual_auxiliary_route_as_capacity(main_remote, aux_remote, expected):
    class Route:
        def public(self):
            return {'generation': {'revision': 9, 'allow_remote': aux_remote,
                'base_url': 'https://aux.example.test/v1' if aux_remote else 'http://127.0.0.1:9000/v1'}}
        def generation_budget_limits(self, **options):
            assert options == {'expected_revision': 9}
            return {'window': 200000, 'reserve': 7000}
    class Models:
        freezes = 0
        def public(self):
            return {'generation': {'allow_remote': main_remote}}
        def freeze_auxiliary_binding(self):
            self.freezes += 1
            return {'actual': True}
        def for_auxiliary(self, binding):
            assert binding == {'actual': True}
            return Route()
    models = Models()
    assert _limits(SimpleNamespace(models=models)) == (193000, 7000, expected)
    assert models.freezes == 1


@pytest.mark.parametrize('change', ['missing', 'duplicate', 'unknown', 'upgrade'])
def test_synthesis_requires_complete_unique_ids_and_inference_label(change):
    claims = [{'id': 'claim-1'}, {'id': 'claim-2'}]
    group = {'label': '综合', 'claim_ids': ['claim-1', 'claim-2'], 'inferences': [],
        'contradictions': [], 'unknowns': []}
    if change == 'missing':
        group['claim_ids'].pop()
    elif change == 'duplicate':
        group['claim_ids'].append('claim-1')
    elif change == 'unknown':
        group['claim_ids'].append('claim-3')
    else:
        group['inferences'] = [{'text': '计划已完成', 'epistemic_status': 'reported_fact', 'claim_ids': ['claim-1']}]
    with pytest.raises(ValueError, match='material_analysis_'):
        parse_synthesis(json.dumps({'groups': [group]}), claims=claims)


def test_time_local_anchor_null_stays_unknown():
    raw = {'events': [{'actors': ['Mira'], 'event_text': 'Mira left yesterday.', 'quote': 'Mira left yesterday.',
        'expression': 'yesterday', 'anchor_quote': None, 'anchor_role': 'local_explicit', 'event_status': 'asserted'}]}
    assert parse_temporal(json.dumps(raw), piece='Mira left yesterday.', offset=0,
        original_title='Material')[0]['event_time']['status'] == 'unknown'


def test_real_capture_body_only_and_independent_sources_reject_revoked_proof(source_env, monkeypatch):
    from backend.memory_app.v2 import material_analysis
    from backend.memory_app.v2.source_sections import SECTIONS
    env = source_env
    class BodyModel(MaterialTransport):
        calls = []
        def complete(self, messages, *, max_tokens, validate_current=None, **options):
            validate_current()
            values = json.loads(messages[-1]['content'])
            self.calls.append(deepcopy(values))
            if 'plan' in values:
                quote = values['source'].splitlines()[0]
                output = {'claims': [claim(quote, quote, actor='主体未确定')]}
            else:
                output = {**PLAN, 'temporal': {'needed': False, 'reason': '正文不涉及日期'}}
            return json.dumps(output), {}
    model = BodyModel()
    env.domains.intake.models = model
    with override(organize='@2'):
        created = asyncio.run(env.domains.intake.add_link({'project_id': 'alpha', 'url': env.item['url']}))
        result = asyncio.run(process_and_confirm(env.domains, created['id'], 'alpha'))
    assert result['status'] == 'confirmed', result
    item = env.records.read('workspace_items', result['id'])
    proof = env.records.read(SECTIONS, result['id'])
    body = proof.payload['body']
    source = item.payload['source_text'][body['start']:body['end']]
    assert '这是原正文中的自然标题' in source and '## 评论区' in source
    assert all(values['source'] == source for values in model.calls)
    assert all('周末已经关门' not in values['source'] and '工作日需要预约' not in values['source']
        for values in model.calls)
    # 独立资格owner不调用可替换的分析返回接口，也不从正文标题猜评论边界。
    monkeypatch.setattr(material_analysis, 'read_document_analysis', lambda *a, **k: pytest.fail('not authoritative'))
    trusted = read_document_analysis_sources(env.records, env.documents, 'alpha', result['document_id'])
    actual = trusted['body_sources'][0]
    assert actual['body'] == body and actual['text'] == item.payload['source_text']
    assert actual['revision'] == item.revision > actual['generation_revision']
    assert actual['section_binding'] == {'id': proof.object_id, 'revision': proof.revision}
    assert len(trusted['analysis_generation_refs']) == 2
    with env.records.begin() as tx:
        tx.delete(SECTIONS, proof.object_id, expected_revision=proof.revision)
        tx.commit()
    with pytest.raises(ValueError, match='material_analysis_binding_changed'):
        read_document_analysis_sources(env.records, env.documents, 'alpha', result['document_id'])


def test_privacy_revoked_during_material_analysis_prevents_l1_publication(runtime):
    from backend.memory_app.v2.privacy import set_private_project, is_private_project
    runtime.model.on_analysis = lambda: set_private_project(runtime.records, 'alpha', True, 0)
    result = process(runtime)
    assert result['status'] == 'failed'
    assert is_private_project(runtime.records, 'alpha')
    assert runtime.documents.list() == () and runtime.records.list(PUBLICATIONS) == ()


@pytest.mark.parametrize('database_relative', ['.rebuild-data/structured-records.sqlite3', 'recognition.sqlite3'])
def test_sources_use_existing_canonical_and_legacy_kernel_paths_readonly(tmp_path, monkeypatch, database_relative):
    monkeypatch.setenv('CHRIPTMAS_APP_ROOT', str(tmp_path))
    records = SQLiteStructuredRecordStore(tmp_path / database_relative)
    documents = SQLiteDocumentRepository(records)
    domains = install_workspace_routes(FastAPI(), runtime_root=tmp_path, records=records,
        models=Model(), documents=documents, service=RecognitionService(records))
    with override(organize='@2'):
        item = asyncio.run(domains.intake.add_text({'project_id': 'alpha', 'text': SOURCE,
            'title': 'Original field notes', 'preserve_text': True}))
        result = asyncio.run(process_and_confirm(domains, item['id'], 'alpha'))
    assert result['status'] == 'confirmed', result
    trusted = read_document_analysis_sources(records, documents, 'alpha', result['document_id'])
    assert len(trusted['analysis_generation_refs']) == 3
    import sqlite3
    isolated_root = tmp_path / 'missing-kernel'
    isolated_path = isolated_root / database_relative
    isolated_path.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(records.database_path) as source_connection, sqlite3.connect(isolated_path) as destination:
        source_connection.backup(destination)
    isolated_records = SQLiteStructuredRecordStore(isolated_path)
    database = isolated_root / '.rebuild-data' / 'ai-turns.sqlite3'
    with pytest.raises(ValueError, match='material_analysis_binding_changed'):
        read_document_analysis_sources(isolated_records, SQLiteDocumentRepository(isolated_records), 'alpha', result['document_id'])
    assert not database.exists()


@pytest.mark.parametrize('capacity', ['missing', None, {'window': '200000', 'reserve': 7000}])
def test_new_flow_requires_actual_capacity_before_dispatch(runtime, capacity):
    runtime.model.generation_budget_limits = None if capacity == 'missing' else lambda **kw: capacity
    result = process(runtime)
    assert result['status'] == 'failed'
    assert result['error'] in {'material_analysis_capacity_unavailable', 'material_analysis_budget_insufficient'}
    assert runtime.model.calls == [] and runtime.records.list('workspace_organize_steps') == ()


def test_auxiliary_binding_change_rejects_result_and_resume_without_extra_dispatch(runtime):
    selected = {'revision': 1}
    runtime.model.freeze_auxiliary_binding = lambda: deepcopy(selected)
    runtime.model.for_auxiliary = lambda binding: runtime.model
    runtime.model.on_analysis = lambda: selected.update(revision=2)
    result = process(runtime)
    assert result['status'] == 'failed' and result['error'] == 'fast_model_binding_changed'
    assert runtime.documents.list() == () and runtime.records.list(PUBLICATIONS) == ()
    assert runtime.records.read(ANALYSES, result['id']).payload['auxiliary_binding'] == {'revision': 1}
    count = len(runtime.model.calls)
    assert count == 2  # 计划成功，分析结果在原有提交守卫中被拒绝。
    resumed = process(runtime)
    assert resumed['status'] == 'failed' and resumed['error'] == 'fast_model_binding_changed'
    assert len(runtime.model.calls) == count


def test_real_manual_draft_edit_previews_and_confirms_without_changing_analysis(runtime):
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
    trusted = read_document_analysis(runtime.records, runtime.documents, 'alpha', result['document_id'])
    assert trusted['analysis'] == original['analysis']
    assert len(read_document_analysis_sources(runtime.records, runtime.documents, 'alpha', result['document_id'])['analysis_generation_refs']) == 3
    assert len(runtime.model.calls) == 3


def test_manual_draft_edit_does_not_accept_tampered_original_generation(runtime):
    ready = process(runtime)
    row = runtime.records.read(ANALYSES, ready['id'])
    payload = deepcopy(row.payload)
    payload['generated_draft']['summary'] = '伪造模型原稿'
    with runtime.records.begin() as tx:
        tx.put(ANALYSES, row.object_id, payload, expected_revision=row.revision)
        tx.commit()
    with pytest.raises(ValueError, match='material_analysis_binding_changed'):
        asyncio.run(runtime.domains.review.save_draft(ready['id'], {
            'project_id': 'alpha', 'expected_revision': ready['revision'], **ready['draft'], 'summary': '合法人工稿'}))
    assert runtime.records.read('workspace_items', ready['id']).revision == ready['revision']


@pytest.mark.parametrize('manifest_change', [None, 'missing', 'invalid', 'source_revoked'])
def test_actual_material_analysis_extract6_and_comment_consumer_full_chain(source_env, manifest_change):
    from backend.memory_app.v2.insight_generation import generate_insights
    from backend.memory_app.v2.insights import insight_view
    from backend.memory_app.v2.memory_turn import MemoryTurn
    from backend.memory_app.v2.source_sections import comment_source_for_candidate, SECTIONS
    from backend.recognition import WorkScope
    env = source_env

    class CombinedModel(MaterialTransport):
        def __init__(self):
            self.calls = []
        def complete(self, messages, *, max_tokens, validate_current=None, **options):
            validate_current()
            values = json.loads(messages[-1]['content'])
            self.calls.append(deepcopy(values))
            if 'experiences' in values:
                source = values['comment_sources'][0]
                output = {'complete': True, 'supports': [], 'insights': [
                    {'origin': 'comment', 'against': 'source_body', 'kind': 'differs', 'relation': 'differs',
                        'text': f'从第{n}个角度核实周末营业情况，不将正文介绍当成所有日期的营业承诺。',
                        'conditions': ['周末出行之前'], 'target_id': None, 'scope_hint': None,
                        'comment': {'source_id': source['source_id'], 'revision': source['revision'],
                            'ordinal': 1, 'quote': '周末已经关门 😀'}, 'body_quote': '营业到晚上。'}
                    for n in range(1, 5)]}
            elif 'plan' in values:
                output = {'claims': [claim('正文介绍营业到晚上，需要按实际日期核实。', '营业到晚上。', actor='店铺')]}
            else:
                output = {**PLAN, 'temporal': {'needed': False, 'reason': '无明确相对日期'}}
            return json.dumps(output, ensure_ascii=False), {'model': 'fake', 'configuration_revision': 1, 'usage': {}}

    model = CombinedModel()
    env.domains.intake.models = model
    with override(organize='@2', extract='@6'):
        item = asyncio.run(env.domains.intake.add_link({'project_id': 'alpha', 'url': env.item['url']}))
        confirmed = asyncio.run(process_and_confirm(env.domains, item['id'], 'alpha'))
        assert confirmed['status'] == 'confirmed', confirmed
        result = generate_insights(model, env.service, env.documents, 'alpha', confirmed['document_id'])
        assert len(result) == 4 and all(row['state'] == 'pending' for row in result)
        assert generate_insights(model, env.service, env.documents, 'alpha', confirmed['document_id']) == result
    assert len(model.calls) == 3 and env.records.list('recognitions') == ()
    candidate = env.records.read('recognition_candidates', result[-1]['id'])
    scope = WorkScope('local-user', 'alpha')
    assert candidate.object_id.endswith('-4')
    assert comment_source_for_candidate(env.records, scope, candidate)['quote'] == '周末已经关门 😀'
    assert insight_view(env.records, scope, candidate.object_id, service=env.service)['comment_source']['quote'] == '周末已经关门 😀'
    if manifest_change == 'source_revoked':
        with env.records.begin() as tx:
            proof = tx.read(SECTIONS, item['id'])
            tx.delete(SECTIONS, proof.object_id, expected_revision=proof.revision)
            tx.commit()
    elif manifest_change is not None:
        # 仅在临时库模拟不可变payload损坏；消费者不能重新构造丢失的权威计划。
        import sqlite3
        turn_id = env.records.list('v2_memory_turn_keys')[0].object_id
        with sqlite3.connect(MemoryTurn.store_for(env.records)._path) as connection:
            if manifest_change == 'missing':
                connection.execute('DELETE FROM ai_turn_immutable_payloads WHERE turn_id=? AND kind=?',
                    (turn_id, 'memory-insights-plan-v2'))
            else:
                connection.execute('UPDATE ai_turn_immutable_payloads SET payload_json=? WHERE turn_id=? AND kind=?',
                    ('{}', turn_id, 'memory-insights-plan-v2'))
    if manifest_change is not None:
        assert comment_source_for_candidate(env.records, scope, candidate) is None
        assert 'comment_source' not in insight_view(env.records, scope, candidate.object_id, service=env.service)


@pytest.mark.parametrize('header,expected', [('', '2024-02-29'), ('Session date: 2024-04-10\n', '2024-04-09'),
    ('Session date: invalid\n', None)])
def test_full_body_record_anchor_is_scoped_to_nearest_header(header, expected):
    body_start = 37
    body = 'Session date: 2024-03-01\n' + 'Context. ' * 700 + '\n\n' + header + 'Sol visited yesterday.'
    piece = header + 'Sol visited yesterday.'
    offset = body_start + body.index(piece)
    quote = 'Sol visited yesterday.'
    anchor = header.strip() if header else 'Session date: 2024-03-01'
    raw = json.dumps({'events': [{'actors': ['Sol'], 'event_text': quote, 'quote': quote,
        'expression': 'yesterday', 'anchor_quote': anchor, 'anchor_role': 'record', 'event_status': 'asserted'}]})
    event = parse_temporal(raw, piece=piece, offset=offset, original_title='Material', full_source=body,
        source_offset=body_start)[0]
    if expected:
        assert event['event_time']['options'][0]['start'] == expected
        evidence = event['anchor_evidence']
        assert body[evidence['start'] - body_start:evidence['end'] - body_start] == anchor
    else:
        assert event['event_time']['status'] == 'unknown'
    # 旧记录日期不能覆盖后续记录头，即便模型把它当锚返回。
    if header:
        stale = json.loads(raw)
        stale['events'][0]['anchor_quote'] = 'Session date: 2024-03-01'
        assert parse_temporal(json.dumps(stale), piece=piece, offset=offset, original_title='Material',
            full_source=body, source_offset=body_start)[0]['event_time']['status'] == 'unknown'


@pytest.mark.parametrize('chosen,expected', [('Session date: 2024-03-04', '2024-03-03'),
    ('Session date: 2024-03-01', None)])
def test_event_quote_including_record_header_uses_expression_position(chosen, expected):
    prefix = 'Session date: 2024-03-01\nEarlier record.\n'
    piece = 'Session date: 2024-03-04\nSol saw the comet yesterday.'
    row = {'actors': ['Sol'], 'event_text': 'Sol saw the comet.', 'quote': piece,
        'expression': 'yesterday', 'anchor_quote': chosen, 'anchor_role': 'record', 'event_status': 'asserted'}
    event = parse_temporal(json.dumps({'events': [row]}), piece=piece, offset=len(prefix),
        original_title='Material', full_source=prefix + piece)[0]
    assert event['computed'] == expected


def test_quoted_record_header_and_quoted_relative_event_cannot_supply_global_anchor():
    body = ('Session date: 2024-03-01\n'
        'He wrote: "Session date: 2024-04-10\nSol visited yesterday."\n'
        'Mira visited yesterday.')
    quoted_event = 'Sol visited yesterday.'
    rows = [dict(actors=['Sol'], event_text=quoted_event, quote=quoted_event, expression='yesterday',
        anchor_quote='Session date: 2024-03-01', anchor_role='record', event_status='asserted'),
        dict(actors=['Mira'], event_text='Mira visited yesterday.', quote='Mira visited yesterday.', expression='yesterday',
            anchor_quote='Session date: 2024-03-01', anchor_role='record', event_status='asserted')]
    events = parse_temporal(json.dumps({'events': rows}), piece=body, offset=0, original_title='Material', full_source=body)
    assert events[0]['event_time']['status'] == 'unknown'
    assert events[1]['event_time']['options'][0]['start'] == '2024-02-29'


def test_full_text_cannot_be_silently_dropped_when_focus_fits_smaller_capacity():
    source = 'A sentence.\n' * 3000
    with pytest.raises(ValueError, match='material_analysis_budget_insufficient'):
        v1.pieces(source, lambda text, offset: v1.focused('analysis', original_title='Long',
            source=text, full_text=source, body_start=0, offset=offset, plan=PLAN),
            input_limit=1500, output_limit=7000, with_offsets=True)


def test_real_long_material_temporal_workers_receive_full_body_and_keep_cross_segment_anchor(runtime):
    class TemporalModel(MaterialTransport):
        def __init__(self):
            self.inputs = []
        def complete(self, messages, *, max_tokens, validate_current=None, **options):
            validate_current()
            values = json.loads(messages[-1]['content'])
            self.inputs.append(deepcopy(values))
            if 'claims' in values:
                output = {'groups': []}
            elif 'plan' in values:
                output = {'claims': []}
            elif 'events' in messages[0]['content']:
                output = {'events': []}
                if 'Sol visited yesterday.' in values['source']:
                    output['events'] = [{'actors': ['Sol'], 'event_text': 'Sol visited yesterday.',
                        'quote': 'Sol visited yesterday.', 'expression': 'yesterday',
                        'anchor_quote': 'Session date: 2024-03-01', 'anchor_role': 'record', 'event_status': 'asserted'}]
            else:
                output = PLAN
            return json.dumps(output), {}
    body = 'Session date: 2024-03-01\n' + 'Context. ' * 1200 + '\n\nSol visited yesterday.'
    runtime.created = asyncio.run(runtime.domains.intake.add_text({'project_id': 'alpha', 'text': body,
        'title': 'Long note', 'preserve_text': True}))
    model = TemporalModel()
    runtime.model = runtime.domains.intake.models = model
    result = confirm(runtime)
    assert result['status'] == 'confirmed', result
    analyzed = read_document_analysis(runtime.records, runtime.documents, 'alpha', result['document_id'])
    assert analyzed['analysis']['temporal'][0]['event_time']['options'][0]['start'] == '2024-02-29'
    assert analyzed['analysis']['temporal'][0]['anchor_evidence']['start'] == 0
    temporal = [values for values in model.inputs if 'focus_range' in values and 'plan' not in values]
    assert len(temporal) > 1
    event_input = next(values for values in temporal if 'Sol visited yesterday.' in values['source'])
    assert 'Session date:' not in event_input['source'] and event_input['full_text'] == body
    assert event_input['focus_range']['start'] > 0
