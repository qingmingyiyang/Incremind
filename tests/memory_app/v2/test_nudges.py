"""核验真实资料、问与回执，轻量装配及模型接入沿用现有测试替身。"""
import asyncio
from copy import deepcopy
from datetime import datetime, timedelta, timezone
import json
import re
from types import SimpleNamespace
from unittest.mock import patch
from uuid import NAMESPACE_URL, uuid5

import pytest
from fastapi import HTTPException

from core.document_engine import SQLiteDocumentRepository
from core.document_engine.ports import DocumentDraft
from backend.memory_app.source_egress import SourceEgressService
from backend.memory_app.v2.auto_confirm import process_and_confirm
from backend.memory_app.v2.library import LibraryRead
from backend.memory_app.v2.policies import override
from backend.memory_app.v2.privacy import set_private_project
from backend.memory_app.v2.profile import PREFIX, confirmed_profile, validate_profile
from backend.memory_app.v2.projects import assign_scene
from backend.memory_app.v2.reminders import ReminderService
from backend.memory_app.v2.signals import SignalService
from backend.recognition import RecognitionConflict, WorkScope
from tests.memory_app.v2.test_workbench_ask import add_document, env, publish


UTC = timezone.utc
LOCAL = timezone(timedelta(hours=8))


@pytest.mark.parametrize('text,day', [
    ('2026年10月10日预约复诊', '2026-10-10'),
    ('2026-10-10预约复诊', '2026-10-10'),
    ('三天后预约复诊', None),
    ('周五预约复诊', None),
    ('10月10日预约复诊', None),
])
def test_candidate_dates_without_a_unique_reference_use_only_complete_calendar_dates(text, day):
    from backend.memory_app.v2.policies import get
    dates = get('nudge', version='@2').dates(text, source_at=None,
        now=datetime(2026, 10, 8, 2, tzinfo=UTC), local_timezone=LOCAL)
    assert [datetime.fromisoformat(row['at']).astimezone(LOCAL).date().isoformat()
            for row in dates] == ([] if day is None else [day])


def test_candidate_date_keeps_invalid_non_none_reference_as_an_explicit_error():
    from backend.memory_app.v2.policies import get
    with pytest.raises(ValueError, match='invalid_nudge_source_reference'):
        get('nudge', version='@2').dates('2026年10月10日预约复诊', source_at='not-a-timestamp',
            now=datetime(2026, 10, 8, 2, tzinfo=UTC), local_timezone=LOCAL)


def _materials(env, *, profile=False):
    for text in (
        'alpha 预算分为固定成本和可变成本，先列出账单',
        'beta 预约应核对日历并准备联系人号码',
        'gamma 出行需要带伞和备用充电器',
    ):
        publish(env, text=text)
    add_document(env, summary='delta 车票属于事先准备的清单', body='delta 车票核对座位')
    add_document(env, summary='echo 行李需要登记重量', body='echo 行李检查拉链')
    if profile:
        for text in ('我习惯先看纸质日历再安排预约', '我出门需要携带自己的蓝色水杯'):
            publish(env, text=text, project='me')
    return '具体 alpha beta gamma delta echo 原文数据'


def _execute(env, plan):
    query = env.domains.query
    identity = query.store_ask_preview(plan)
    result = asyncio.run(query.execute_ask(identity, plan['project_id'], plan['question'], True))
    receipt = env.records.read('workspace_ask_receipts', identity)
    turn_id = 'turn-' + uuid5(NAMESPACE_URL, 'workspace-ask:' + identity).hex
    request = query.answer_turns.application.state.ai_turn_store.get_request(turn_id)
    assert request['desired_outcome'] == 'project.answer'
    assert request['scope']['project_id'] == plan['project_id']
    assert receipt.payload['status'] == 'completed'
    return result, receipt, request


def _without_expiration(plan):
    return {key: value for key, value in plan.items()
            if key not in {'expires_at', 'expires_monotonic'}}


def test_query_default_none_preserves_every_frozen_plan_field(env):
    question = _materials(env, profile=True)
    collected = env.domains.query.collect_candidates('alpha', question)
    original = env.domains.query.prepare_ask('alpha', question, collected=collected)
    assert len(original['chosen']) >= 4
    assert len(original['profile']['items']) == 2
    explicit = env.domains.query.prepare_ask('alpha', question, collected=collected,
                                            evidence_limit=None, profile_limit=None)
    assert _without_expiration(original) == _without_expiration(explicit)


def test_query_total_three_is_enforced_before_real_answer_freeze_and_wire(env):
    question = _materials(env)
    plan = env.domains.query.prepare_ask('alpha', question, evidence_limit=3, profile_limit=1)
    assert len(plan['chosen']) == 3
    frozen = deepcopy(plan['chosen'])
    result, receipt, _ = _execute(env, plan)
    assert len(receipt.payload['sources']) == 3
    assert len(result['sources']) == 3
    assert {row['id'] for row in result['sources']} == {row['id'] for row in frozen}
    assert len(re.findall(r'^\[\d+\]', env.model.messages[-1]['content'], re.MULTILINE)) == 3
    assert env.model.calls == 1


def test_query_total_three_reserves_one_qualified_profile_and_rebuilds_sent_text(env):
    question = _materials(env, profile=True)
    full = confirmed_profile(env.records, env.service)
    plan = env.domains.query.prepare_ask('alpha', question, evidence_limit=3, profile_limit=1)
    profile = plan['profile']
    assert len(plan['chosen']) == 2 and len(profile['items']) == 1
    assert profile['basis'] == full['basis'] and len(profile['basis']['items']) == 2
    excluded = next(item for item in full['items'] if item['id'] != profile['items'][0]['id'])
    result, receipt, _ = _execute(env, plan)
    assert len(receipt.payload['sources']) == 3
    assert len(result['sources']) == 2
    assert profile['text'] == PREFIX + '- ' + profile['items'][0]['content'] + '\n'
    assert env.model.messages[0]['content'] == profile['text']
    assert excluded['content'] not in '\n'.join(row['content'] for row in env.model.messages)


def test_query_zero_budget_sends_no_profile_text_items_or_rank_instruction(env):
    question = _materials(env, profile=True)
    full = confirmed_profile(env.records, env.service)
    before = [(row.object_id, row.revision, row.payload) for row in env.records.list('v2_profile_blocks')]
    plan = env.domains.query.prepare_ask('alpha', question, evidence_limit=0, profile_limit=1)
    profile = plan['profile']
    assert plan['chosen'] == []
    assert profile['text'] == '' and profile['tokens'] == 0 and profile['count'] == 0
    assert profile['items'] == [] and not profile.get('instruction')
    assert profile['basis'] == full['basis']
    assert [(row.object_id, row.revision, row.payload) for row in env.records.list('v2_profile_blocks')] == before
    assert env.model.calls == 0


@pytest.mark.parametrize('count', [0, 1, 2])
def test_bounded_profile_rebuilds_complete_atomic_text_and_preserves_authority(env, count):
    from backend.memory_app.v2.profile import bounded_profile
    for text in ('我习惯先看纸质日历再安排预约', '我出门需要携带自己的蓝色水杯'):
        publish(env, text=text, project='me')
    full = confirmed_profile(env.records, env.service)
    original = deepcopy(full)
    bounded = bounded_profile(full, count)
    expected = PREFIX + ''.join('- ' + item['content'] + '\n' for item in full['items'][:count]) if count else ''
    assert bounded['text'] == expected and bounded['count'] == count
    assert bounded['items'] == full['items'][:count]
    assert bounded['basis'] == full['basis'] and full == original
    from backend.memory_app.v2.budget import text_tokens
    assert bounded['tokens'] == text_tokens(expected)
    if count == 0:
        assert not bounded.get('instruction')
    validate_profile(env.records, env.service, bounded)


@pytest.mark.parametrize('value', [-1, True, 1.5, '3', [], {}])
def test_query_invalid_evidence_budget_has_context_and_no_model(env, value):
    with pytest.raises(ValueError, match='invalid_query_evidence_limit'):
        env.domains.query.prepare_ask('alpha', 'hello', evidence_limit=value)
    assert env.model.calls == 0


@pytest.mark.parametrize('value', [-1, False, 1.5, '1', []])
def test_query_invalid_profile_budget_has_context_and_no_model(env, value):
    with pytest.raises(ValueError, match='invalid_query_profile_limit'):
        env.domains.query.prepare_ask('alpha', 'hello', evidence_limit=3, profile_limit=value)
    assert env.model.calls == 0


@pytest.mark.parametrize('change', ['profile_private', 'ordinary_private', 'source_drift'])
def test_bounded_query_keeps_real_final_privacy_and_source_checks(env, change):
    question = _materials(env, profile=True)
    plan = env.domains.query.prepare_ask('alpha', question, evidence_limit=3, profile_limit=1)
    identity = env.domains.query.store_ask_preview(plan)
    if change == 'profile_private':
        env.model.before = lambda: set_private_project(env.records, 'me', True, 0)
    elif change == 'ordinary_private':
        env.model.before = lambda: set_private_project(env.records, 'alpha', True, 0)
    else:
        selected = plan['chosen'][0]
        scope = selected.get('scope', plan['scope'])
        authority = SourceEgressService(env.records)
        env.model.before = lambda: authority.set_policy(scope, 'recognition', selected['id'],
                                                        selected['entry']['revision'], 0, [])
    with pytest.raises(HTTPException) as failure:
        asyncio.run(env.domains.query.execute_ask(identity, 'alpha', question, True))
    assert failure.value.status_code == 409
    assert env.model.calls == 0
    assert env.records.read('workspace_ask_receipts', identity).payload['status'] == 'failed'


def test_bounded_profile_retains_unselected_items_in_final_source_basis(env):
    from backend.memory_app.v2.profile import bounded_profile
    for text in ('我习惯先看纸质日历再安排预约', '我出门需要携带自己的蓝色水杯'):
        publish(env, text=text, project='me')
    full = confirmed_profile(env.records, env.service)
    bounded = bounded_profile(full, 1)
    unused = full['items'][1]
    SourceEgressService(env.records).set_policy(WorkScope('local-user', 'me'), 'recognition',
                                               unused['id'], unused['revision'], 0, [])
    with pytest.raises(RecognitionConflict, match='profile changed'):
        validate_profile(env.records, env.service, bounded)


def test_shared_budget_covers_appended_real_situation_methods(env):
    with override(retrieve='@2', compose='@2'):
        for text in ('比较实用程度', '先问最近爱好', '留出换货余地', '考虑收纳空间'):
            scope = WorkScope('local-user', 'alpha')
            experience = env.service.stage_experience(scope=scope, content='Synthetic method source')
            proposal = env.service.propose(scope=scope, content=text, conditions=['挑礼物时'],
                                          source_experience_ids=[experience])
            env.service.publish(scope=scope, candidate_id=proposal.id, expected_revision=1, reviewer='local-user')
        question = '给小王选生日礼物？'
        original = env.domains.query.prepare_ask('alpha', question)
        assert len([row for row in original['chosen'] if row.get('supplemented')]) == 3
        plan = env.domains.query.prepare_ask('alpha', question, evidence_limit=1, profile_limit=0)
        assert len(plan['chosen']) == 1 and plan['chosen'][0]['supplemented']
        _, receipt, _ = _execute(env, plan)
        assert len(receipt.payload['sources']) == 1


def test_shared_budget_covers_appended_real_inspiration_sources(env):
    from tests.memory_app.v2.test_inspirations import capture
    with override(scope='@3', compose='@4'):
        publish(env)
        for text in ('alpha beta gamma 用折纸讲解', 'alpha beta gamma 用木偶演示'):
            capture(env, text)
        question = 'alpha beta gamma 有什么点子？'
        original = env.domains.query.prepare_ask('alpha', question)
        assert any(row.get('inspiration') for row in original['chosen'])
        plan = env.domains.query.prepare_ask('alpha', question, evidence_limit=1, profile_limit=0)
        assert len(plan['chosen']) == 1
        assert all(not row.get('inspiration') for row in plan['chosen'])
        _, receipt, _ = _execute(env, plan)
        assert len(receipt.payload['sources']) == 1


@pytest.fixture
def nudges(env):
    from backend.memory_app.v2.nudges import NudgeService
    clock = SimpleNamespace(at=datetime(2026, 10, 8, 2, tzinfo=UTC))
    signals = SignalService(env.records, now=lambda: clock.at)
    reminders = ReminderService(env.records, now=lambda: clock.at, local_timezone=LOCAL, policy_version='@1')
    owner = NudgeService(env.records,
        library=LibraryRead(env.records, env.service, env.documents, env.domains),
        query=env.domains.query, models=env.model, signals=signals, reminders=reminders,
        now=lambda: clock.at, local_timezone=LOCAL, policy_version='@2')
    return SimpleNamespace(owner=owner, clock=clock, signals=signals, reminders=reminders, env=env)


def _dated_document(env, text, *, project='alpha', scene=None, created_at='2026-10-08T00:00:00+00:00', confirmed_at=None):
    intake, allowed = env.model.intake, env.model.allowed
    try:
        env.model.intake, env.model.allowed = True, True
        # 原件和整理稿使用各自保存时钟，默认同步，日期推导由真实消费者执行。
        with patch('backend.memory_app.workspace_items._now', return_value=created_at), \
                patch('backend.memory_app.workspace_confirmation._now', return_value=confirmed_at if confirmed_at is not None else created_at):
            item = asyncio.run(env.domains.intake.add_text({'project_id': project, 'text': text}))
            ready = asyncio.run(env.domains.intake.process(item['id'], {'project_id': project}))
            draft = deepcopy(ready['draft'])
            draft.update(title='Synthetic date', summary=text,
                facts=[{'text': text, 'evidence': {'start': 0, 'end': len(text), 'quote': text}}])
            asyncio.run(env.domains.review.save_draft(item['id'], {
                **draft, 'project_id': project, 'expected_revision': ready['revision']}))
            result = asyncio.run(process_and_confirm(env.domains, item['id'], project))
            assert result['status'] == 'confirmed'
            identity = result['document_id']
    finally:
        env.model.intake, env.model.allowed = intake, allowed
    if scene:
        assign_scene(env.records, 'document', identity, project, scene)
    return identity


def test_nudge_dates_read_real_qualified_library_and_do_not_modify_original_facts(nudges):
    env = nudges.env
    confirmed, _ = publish(env, text='小王生日是10月10日', project='alpha')
    scope = WorkScope('local-user', 'alpha')
    experience = env.service.stage_experience(scope=scope, content='Synthetic unconfirmed source')
    env.service.propose(scope=scope, content='小李生日是10月11日', source_experience_ids=[experience])
    document = _dated_document(env, '2026年10月12日到医院复诊', scene='预约')
    _dated_document(env, '2026年10月13日乘车', project='beta')
    before = [(name, row.object_id, row.revision, row.payload)
              for name in ('recognitions', 'recognition_candidates', 'documents', 'workspace_items')
              for row in env.records.list(name)]
    dates = nudges.owner.refresh('alpha')
    assert {(row['source_kind'], row['source_id']) for row in dates} == {
        ('recognition', confirmed.id), ('document', document)}
    assert next(row for row in dates if row['source_id'] == document)['scene'] == '预约'
    assert [(name, row.object_id, row.revision, row.payload)
            for name in ('recognitions', 'recognition_candidates', 'documents', 'workspace_items')
            for row in env.records.list(name)] == before
    assert len(env.records.list('v2_dates')) == 2


def test_nudge_relative_date_is_anchored_to_original_material_day_across_rebuilds(nudges):
    identity = _dated_document(nudges.env, '三天后预约洗牙', created_at='2026-10-07T00:00:00+00:00')
    first = nudges.owner.refresh('alpha')
    event = next(row for row in first if row['source_id'] == identity)
    assert datetime.fromisoformat(event['at']).astimezone(LOCAL).date().isoformat() == '2026-10-10'
    nudges.clock.at += timedelta(days=1)
    second = nudges.owner.refresh('alpha')
    assert next(row for row in second if row['source_id'] == identity)['at'] == event['at']
    assert next(row for row in second if row['source_id'] == identity)['id'] == event['id']


def test_relative_date_keeps_original_capture_day_when_filing_happens_a_day_later(nudges):
    env = nudges.env
    identity = _dated_document(env, '三天后预约洗牙',
        created_at='2026-10-07T00:00:00+00:00', confirmed_at='2026-10-08T00:00:00+00:00')
    document = deepcopy(env.documents.read(identity))
    refs = [ref for ref in document['source_refs']
            if ref['locator'] == 'workspace://' + ref['source_id']]
    assert len(refs) == 1
    item = env.records.read('workspace_items', refs[0]['source_id'])
    assert item.payload['document_id'] == identity and item.payload['status'] == 'confirmed'
    assert item.payload['created_at'] == '2026-10-07T00:00:00+00:00'
    assert document['created_at'] == '2026-10-08T00:00:00+00:00'
    original = deepcopy(item.payload)
    first = nudges.owner.refresh('alpha')
    event = next(row for row in first if row['source_id'] == identity)
    assert datetime.fromisoformat(event['at']).astimezone(LOCAL).date().isoformat() == '2026-10-10'
    nudges.clock.at += timedelta(days=1)
    second = nudges.owner.refresh('alpha')
    repeated = next(row for row in second if row['source_id'] == identity)
    assert repeated['at'] == event['at'] and repeated['id'] == event['id']
    current = env.records.read('workspace_items', item.object_id)
    assert current.revision == item.revision and current.payload == original
    assert env.documents.read(identity) == document and env.model.calls == 0


def test_temporary_original_guards_never_enter_public_or_persisted_nudge_projections(nudges):
    from backend.memory_app.original_sources import document_roots, original
    env = nudges.env
    publish(env, text='整理资料时先核对联系人', project='alpha')
    identity = _dated_document(env, '2026年10月10日预约复诊', scene='旅行/上海')
    document = deepcopy(env.documents.read(identity))
    owner_id = next(ref['source_id'] for ref in document['source_refs']
                    if ref['locator'] == 'workspace://' + ref['source_id'])
    owner = env.records.read('workspace_items', owner_id)
    assert owner.payload['status'] == 'confirmed' and owner.payload['document_id'] == identity
    scope = WorkScope('local-user', 'alpha')
    originals = {}
    for kind, source_id, revision in document_roots(env.records, scope, document['source_refs']):
        if kind == 'original_source':
            source = original(env.records, scope, kind, source_id)
            assert source.revision == revision
            originals[(kind, source_id)] = (deepcopy(source.payload), source.revision)
    assert originals
    facts = [(name, row.object_id, row.revision, deepcopy(row.payload))
             for name in ('recognitions', 'recognition_candidates', 'documents', 'workspace_items')
             for row in env.records.list(name)]
    prepared = next(source for source in nudges.owner._sources('alpha') if source['source_id'] == identity)
    assert {'_guard', '_date_guard'} <= prepared.keys()
    assert {root[0] for root in prepared['_date_guard']['closure']} == {'original_item', 'original_source'}
    guard_keys = {'_guard', '_date_guard', '_source_input', '_source_text'}
    def assert_no_guard(value):
        # 同时检查嵌套字段，避免内部原件闭包随公开投影或回执被间接保存。
        if isinstance(value, dict):
            assert guard_keys.isdisjoint(value)
            for nested in value.values():
                assert_no_guard(nested)
        elif isinstance(value, (list, tuple)):
            for nested in value:
                assert_no_guard(nested)
    dates = nudges.owner.refresh('alpha')
    assert len(dates) == 1 and dates[0]['source_id'] == identity
    places = nudges.owner.place_candidates('alpha')
    assert len(places) == 1 and places[0]['source_id'] == identity and places[0]['city_id'] == 1796236
    env.model.allowed = False
    delivered = asyncio.run(nudges.owner.deliver('alpha', city='上海'))
    assert len(delivered) == 2 and {row['kind'] for row in delivered} == {'date', 'place'}
    assert all(row['source_id'] == identity and row['state'] == 'delivered'
               and row['model_used'] is False and row['egress_receipt_id'] is None for row in delivered)
    public = nudges.owner.list('alpha')
    assert public == delivered
    stored_dates, stored_nudges = env.records.list('v2_dates'), env.records.list('v2_nudges')
    assert len(stored_dates) == 1 and len(stored_nudges) == 2
    for projection in (dates, places, delivered, public,
                       [row.payload for row in stored_dates], [row.payload for row in stored_nudges]):
        assert_no_guard(projection)
    assert [(name, row.object_id, row.revision, row.payload)
            for name in ('recognitions', 'recognition_candidates', 'documents', 'workspace_items')
            for row in env.records.list(name)] == facts
    assert env.documents.read(identity) == document
    for (kind, source_id), (source, revision) in originals.items():
        current = original(env.records, scope, kind, source_id)
        assert current.payload == source
        assert current.revision == revision
    assert env.records.list('workspace_ask_receipts') == () and env.model.calls == 0


def test_workspace_confirmation_source_alias_and_owner_supply_one_capture_reference(nudges):
    from backend.memory_app.original_sources import document_roots, original
    env = nudges.env
    identity = _dated_document(env, '三天后预约洗牙',
        created_at='2026-10-07T00:00:00+00:00', confirmed_at='2026-10-08T00:00:00+00:00')
    document = deepcopy(env.documents.read(identity))
    scope = WorkScope('local-user', 'alpha')
    roots = document_roots(env.records, scope, document['source_refs'])
    assert len(roots) == 2 and {root[0] for root in roots} == {'original_item', 'original_source'}
    owner_id = next(root[1] for root in roots if root[0] == 'original_item')
    alias_id = next(root[1] for root in roots if root[0] == 'original_source')
    alias = original(env.records, scope, 'original_source', alias_id)
    assert alias.payload['identity_method'] == 'workspace_confirmation'
    assert alias.payload['workspace_item_id'] == owner_id
    assert alias.payload['created_at'] == '2026-10-08T00:00:00+00:00'
    assert env.records.read('workspace_items', owner_id).payload['created_at'] == '2026-10-07T00:00:00+00:00'
    events = nudges.owner.refresh('alpha')
    assert len(events) == 1 and events[0]['source_id'] == identity
    assert events[0]['source_at'] == '2026-10-07T00:00:00+00:00'
    assert datetime.fromisoformat(events[0]['at']).astimezone(LOCAL).date().isoformat() == '2026-10-10'
    assert env.documents.read(identity) == document and env.model.calls == 0


def _document_from_retained_refs(env, refs):
    texts = ['2026年10月10日预约复诊', '三天后领取药品', '周五出门', '10月10日领取检查结果']
    repository = SQLiteDocumentRepository(env.records, now='2026-10-08T00:00:00+00:00')
    return repository.create(DocumentDraft(title='Synthetic reference dates', document_type='synthetic-dates',
        markdown='# Synthetic\n\n## 关键事实\n' + '\n'.join('- ' + text for text in texts),
        source_refs=tuple(refs), project_id='alpha'))['id']


def test_multiple_independent_originals_do_not_guess_a_relative_date_from_the_earliest_capture(nudges):
    from backend.memory_app.original_sources import document_roots
    env, refs, originals = nudges.env, [], {}
    for index, day in enumerate((6, 7)):
        identity = 'independent-original-' + str(index)
        body = {'id': identity, 'project_id': 'alpha', 'title': identity, 'type': 'text',
            'created_at': f'2026-10-{day:02}T00:00:00+00:00',
            'metadata': {'content_snapshot': 'Synthetic retained original'}}
        env.domains.query.source_store.write('sources', identity, body, expected_revision=0)
        originals[identity] = (deepcopy(body), env.domains.query.source_store.revision('sources', identity))
        refs.append({'source_id': identity, 'locator': 'source://' + identity})
    document = _document_from_retained_refs(env, refs)
    original_document = deepcopy(env.documents.read(document))
    roots = document_roots(env.records, WorkScope('local-user', 'alpha'), original_document['source_refs'])
    assert len(roots) == 2 and all(root[0] == 'original_source' for root in roots)
    assert document in nudges.owner.library.docs('alpha')
    events = nudges.owner.refresh('alpha')
    assert len(events) == 1 and events[0]['span'] == '2026年10月10日'
    assert datetime.fromisoformat(events[0]['at']).astimezone(LOCAL).date().isoformat() == '2026-10-10'
    assert env.documents.read(document) == original_document
    for identity, (body, revision) in originals.items():
        assert env.domains.query.source_store.read('sources', identity) == body
        assert env.domains.query.source_store.revision('sources', identity) == revision
    assert env.model.calls == 0


def test_historical_document_without_retained_l0_does_not_supply_a_relative_reference(nudges):
    from backend.memory_app.original_sources import document_roots
    env, missing = nudges.env, 'unretained-historical-original'
    document = _document_from_retained_refs(env, [{'source_id': missing, 'locator': 'source://' + missing}])
    original_document = deepcopy(env.documents.read(document))
    assert env.domains.query.source_store.read('sources', missing) is None
    assert document_roots(env.records, WorkScope('local-user', 'alpha'),
        original_document['source_refs'], optional=True) == ()
    assert document in nudges.owner.library.docs('alpha')
    events = nudges.owner.refresh('alpha')
    assert len(events) == 1 and events[0]['span'] == '2026年10月10日'
    assert datetime.fromisoformat(events[0]['at']).astimezone(LOCAL).date().isoformat() == '2026-10-10'
    assert env.documents.read(document) == original_document and env.model.calls == 0


@pytest.mark.parametrize('captured_at', [None, 'not-a-timestamp'])
def test_unique_original_with_missing_or_invalid_capture_time_fails_with_context(nudges, captured_at):
    from backend.memory_app.original_sources import document_roots
    env, identity = nudges.env, 'single-original-with-invalid-capture'
    body = {'id': identity, 'project_id': 'alpha', 'title': identity, 'type': 'text',
            'metadata': {'content_snapshot': 'Synthetic retained original'}}
    if captured_at is not None:
        body['created_at'] = captured_at
    env.domains.query.source_store.write('sources', identity, body, expected_revision=0)
    document = _document_from_retained_refs(env, [{'source_id': identity, 'locator': 'source://' + identity}])
    original_document = deepcopy(env.documents.read(document))
    roots = document_roots(env.records, WorkScope('local-user', 'alpha'), original_document['source_refs'])
    assert len(roots) == 1 and roots[0][:2] == ('original_source', identity)
    assert document in nudges.owner.library.docs('alpha')
    assert env.domains.query.source_store.read('sources', identity).get('created_at') == captured_at
    with pytest.raises(HTTPException) as error:
        nudges.owner.refresh('alpha')
    assert error.value.status_code == 409 and error.value.detail == 'invalid_nudge_source_reference'
    assert env.records.list('v2_dates') == () and env.documents.read(document) == original_document
    assert env.domains.query.source_store.read('sources', identity) == body and env.model.calls == 0


@pytest.mark.parametrize('change', [{'title': 'Synthetic concurrent title'}, {'document_id': 'different-document'}])
def test_prepared_original_revision_or_document_binding_change_is_rejected_in_the_index_transaction(nudges, change):
    env = nudges.env
    identity = _dated_document(env, '三天后预约洗牙')
    document = deepcopy(env.documents.read(identity))
    owner_id = next(ref['source_id'] for ref in document['source_refs']
                    if ref['locator'] == 'workspace://' + ref['source_id'])
    owner = env.records.read('workspace_items', owner_id)
    prepared = nudges.owner._sources('alpha')
    assert any(source['source_id'] == identity for source in prepared)
    assert env.records.list('v2_dates') == ()
    env.domains.intake.items.update(owner_id, 'alpha', {'confirmed'}, **change)
    current = env.records.read('workspace_items', owner_id)
    assert current.revision == owner.revision + 1 and all(current.payload[key] == value for key, value in change.items())
    with pytest.raises(HTTPException) as error:
        # 直接核真实刷新调用的同一事务校验器，不注入或替换策略、消费者和仓储。
        with env.records.begin() as tx:
            nudges.owner._validate_sources(tx, prepared)
            tx.commit()
    assert error.value.status_code == 409
    assert env.records.list('v2_dates') == () and env.documents.read(identity) == document
    assert env.model.calls == 0


def test_confirmed_birthday_birth_year_is_rebuilt_as_the_current_anniversary(nudges):
    birthday, _ = publish(nudges.env, text='小王生日是1990年10月10日')
    dates = nudges.owner.refresh('alpha')
    event = next(row for row in dates if row['source_id'] == birthday.id)
    assert datetime.fromisoformat(event['at']).astimezone(LOCAL).date().isoformat() == '2026-10-10'
    assert event['annual'] is True


def test_nudge_delivery_cap_is_global_across_projects_and_event_days(nudges):
    env = nudges.env
    env.model.allowed = False
    for project, days in [('alpha', (1, 2)), ('beta', (3, 4))]:
        for day in days:
            _dated_document(env, f'2026年10月{8 + day}日预约检查', project=project)
    first = asyncio.run(nudges.owner.deliver('alpha'))
    second = asyncio.run(nudges.owner.deliver('beta'))
    assert len(first) == 2 and second == []
    assert len({row['at'] for row in first}) == 2
    assert {datetime.fromisoformat(row['delivery_at']).astimezone(LOCAL).date().isoformat()
            for row in first} == {'2026-10-08'}
    assert len(env.records.list('v2_nudges')) == 2 and env.model.calls == 0


def test_due_user_reminders_rebuild_into_projection_and_bypass_zero_daily_limit(nudges):
    env = nudges.env
    nudges.owner.set_limit(0, expected_revision=0)
    nudges.clock.at = datetime(2026, 10, 8, 0, tzinfo=UTC)
    for number in range(3):
        nudges.reminders.create(project_id='alpha', scene=None, text='提醒我今天早上九点喝水',
                               turn_id=f'turn-{number}')
    facts = [(row.object_id, row.revision, row.payload) for row in env.records.list('v2_reminders')]
    nudges.clock.at += timedelta(hours=2)
    first = asyncio.run(nudges.owner.deliver('alpha'))
    second = asyncio.run(nudges.owner.deliver('alpha'))
    assert len(first) == 3 and all(row['kind'] == 'reminder' for row in first)
    assert {row['id'] for row in first} == {row['id'] for row in second}
    assert len(env.records.list('v2_nudges')) == 3
    assert [(row.object_id, row.revision, row.payload) for row in env.records.list('v2_reminders')] == facts
    assert env.model.calls == 0


def test_private_project_never_creates_nudge_even_for_due_user_reminder(nudges):
    _dated_document(nudges.env, '2026年10月10日预约复诊')
    nudges.clock.at = datetime(2026, 10, 8, 0, tzinfo=UTC)
    nudges.reminders.create(project_id='alpha', scene=None, text='提醒我今天早上九点喝水',
                           turn_id='turn-private')
    nudges.clock.at += timedelta(hours=2)
    set_private_project(nudges.env.records, 'alpha', True, 0)
    assert nudges.owner.refresh('alpha') == []
    assert asyncio.run(nudges.owner.deliver('alpha')) == []
    assert nudges.env.records.list('v2_nudges') == ()
    assert nudges.env.model.calls == 0


@pytest.mark.parametrize('days,expected', [(0, True), (13, True), (14, False), (-1, False)])
def test_real_date_index_uses_the_fourteen_day_local_calendar_window(nudges, days, expected):
    local = nudges.clock.at.astimezone(LOCAL).date() + timedelta(days=days)
    identity = _dated_document(nudges.env, local.isoformat() + '预约复诊')
    assert any(row['source_id'] == identity for row in nudges.owner.refresh('alpha')) is expected


def test_date_rebuild_removes_archived_projection_without_deleting_document(nudges):
    identity = _dated_document(nudges.env, '2026年10月10日预约复诊')
    assert len(nudges.owner.refresh('alpha')) == 1
    nudges.env.documents.archive(identity, expected_revision=1)
    assert nudges.owner.refresh('alpha') == []
    assert nudges.env.records.list('v2_dates') == ()
    assert nudges.env.documents.read(identity)['status'] == 'archived'


def test_date_rebuild_excludes_manually_forgotten_document_kept_on_the_bookshelf(nudges):
    identity = _dated_document(nudges.env, '2026年10月10日预约复诊')
    assert len(nudges.owner.refresh('alpha')) == 1
    with nudges.env.records.begin() as tx:
        tx.put('v2_document_recall', identity, {'state': 'forgotten', 'by': 'user'}, expected_revision=0)
        tx.commit()
    assert nudges.owner.refresh('alpha') == []
    assert nudges.env.documents.read(identity) is not None


@pytest.mark.parametrize('action', ['opened', 'ignored', 'closed'])
def test_feedback_is_scoped_cas_and_original_facts_stay_unchanged(nudges, action):
    env = nudges.env
    env.model.allowed = False
    identity = _dated_document(env, '2026年10月10日预约复诊')
    original = deepcopy(env.documents.read(identity))
    row = asyncio.run(nudges.owner.deliver('alpha'))[0]
    with pytest.raises(HTTPException) as missing:
        nudges.owner.feedback(row['id'], project_id='beta', action=action, expected_revision=row['revision'])
    assert missing.value.status_code == 404
    saved = nudges.owner.feedback(row['id'], project_id='alpha', action=action, expected_revision=row['revision'])
    assert saved['action'] == action and saved['revision'] == row['revision'] + 1
    with pytest.raises(HTTPException) as stale:
        nudges.owner.feedback(row['id'], project_id='alpha', action=action, expected_revision=row['revision'])
    assert stale.value.status_code == 409
    assert env.documents.read(identity) == original


def test_real_delivery_keeps_the_same_future_event_quiet_until_seven_days(nudges):
    nudges.env.model.allowed = False
    source = _dated_document(nudges.env, '2026年10月20日预约复诊')
    original, reference = deepcopy(nudges.env.documents.read(source)), nudges.clock.at
    first = asyncio.run(nudges.owner.deliver('alpha'))
    assert len(first) == 1 and first[0]['source_id'] == source
    event = first[0]['event_id']
    for days in (1, 6):
        nudges.clock.at = reference + timedelta(days=days)
        dates = nudges.owner.refresh('alpha')
        assert any(row['id'] == event and row['at'] == first[0]['at'] for row in dates)
        assert asyncio.run(nudges.owner.deliver('alpha')) == []
    nudges.clock.at = reference + timedelta(days=7)
    dates = nudges.owner.refresh('alpha')
    assert any(row['id'] == event and datetime.fromisoformat(row['at']) > nudges.clock.at for row in dates)
    repeated = asyncio.run(nudges.owner.deliver('alpha'))
    assert len(repeated) == 1 and repeated[0]['event_id'] == event
    assert len(nudges.env.records.list('v2_nudges')) == 2
    assert nudges.env.documents.read(source) == original and nudges.env.model.calls == 0


@pytest.mark.parametrize('action,expected_kind', [('opened', 'date'), ('closed', 'place')])
def test_real_feedback_changes_date_priority_against_an_eligible_local_city(nudges, action, expected_kind):
    env = nudges.env
    env.model.allowed = False
    dates = {_dated_document(env, text) for text in
        ('2026年10月8日预约复诊', '2026年10月8日领取药品')}
    city = _dated_document(env, '准备行李和车票', scene='旅行/纽约')
    originals = {identity: deepcopy(env.documents.read(identity)) for identity in dates | {city}}
    assert any(row['source_id'] == city for row in nudges.owner.place_candidates('alpha'))
    # 两个今日日期先于尚未有反馈的城市，证明默认排序的真实对照。
    first = asyncio.run(nudges.owner.deliver('alpha', city='纽约'))
    assert len(first) == 2 and all(row['kind'] == 'date' for row in first)
    assert {row['source_id'] for row in first} == dates
    for row in first:
        nudges.owner.feedback(row['id'], project_id='alpha', action=action, expected_revision=row['revision'])
    nudges.clock.at += timedelta(days=1)
    next_date = _dated_document(env, '2026年10月9日预约复诊')
    nudges.owner.set_limit(1, expected_revision=0)
    selected = asyncio.run(nudges.owner.deliver('alpha', city='纽约'))
    assert len(selected) == 1 and selected[0]['kind'] == expected_kind
    assert selected[0]['source_id'] == (city if action == 'closed' else next_date)
    assert {identity: env.documents.read(identity) for identity in originals} == originals
    assert env.model.calls == 0


def test_closed_user_reminder_does_not_lower_a_new_due_reminder_under_zero_limit(nudges):
    nudges.clock.at = datetime(2026, 10, 8, 0, tzinfo=UTC)
    nudges.owner.set_limit(0, expected_revision=0)
    first_fact = nudges.reminders.create(project_id='alpha', scene=None,
        text='提醒我今天早上九点喝水', turn_id='turn-closed-reminder')
    nudges.clock.at += timedelta(hours=2)
    first = asyncio.run(nudges.owner.deliver('alpha'))
    assert len(first) == 1 and first[0]['event_id'] == first_fact['id']
    nudges.owner.feedback(first[0]['id'], project_id='alpha', action='closed', expected_revision=first[0]['revision'])
    next_fact = nudges.reminders.create(project_id='alpha', scene=None,
        text='提醒我今天上午十一点买药', turn_id='turn-next-reminder')
    nudges.clock.at += timedelta(hours=1)
    delivered = asyncio.run(nudges.owner.deliver('alpha'))
    assert len(delivered) == 1 and delivered[0]['event_id'] == next_fact['id']
    assert delivered[0]['kind'] == 'reminder' and delivered[0]['text'] == next_fact['text']
    assert nudges.reminders.read(first_fact['id'], project_id='alpha') == first_fact
    assert nudges.reminders.read(next_fact['id'], project_id='alpha') == next_fact
    assert nudges.env.model.calls == 0


@pytest.mark.parametrize('limit', [0, 1, 2, 5])
def test_real_delivery_uses_configured_zero_to_five_daily_limit(nudges, limit):
    nudges.env.model.allowed = False
    for day in range(9, 15):
        _dated_document(nudges.env, f'2026年10月{day}日预约复诊')
    saved = nudges.owner.set_limit(limit, expected_revision=0)
    assert saved['limit'] == limit and saved['revision'] == 1
    assert len(asyncio.run(nudges.owner.deliver('alpha'))) == limit
    with pytest.raises(HTTPException) as stale:
        nudges.owner.set_limit(2, expected_revision=0)
    assert stale.value.status_code == 409


def _remember_times(nudges, count, hour, *, weekend=False):
    from backend.memory_app.v2.workbench import persist_workbench_turn
    day = nudges.clock.at.astimezone(LOCAL).date() - timedelta(days=1)
    dates = []
    while len(dates) < (count + 1) // 2:
        if (day.weekday() >= 5) == weekend:
            dates.append(day)
        day -= timedelta(days=1)
    with nudges.env.records.begin() as tx:
        for index in range(count):
            at = datetime.combine(dates[index // 2], datetime.min.time(), tzinfo=LOCAL).replace(hour=hour)
            identity = f'usage-{weekend}-{index}'
            persist_workbench_turn(tx, turn_id=identity, project='alpha', thread_id='thread-' + identity,
                cleaned='Synthetic usage round', now=at.isoformat(), created_at=at.isoformat(),
                intent='remember', receipt={}, item_id='item-' + identity, item={'title':'Synthetic'},
                instance='synthetic', run_id=None, title_prefix=None, replace_turn=None, research_state=None)
        tx.commit()


@pytest.mark.parametrize('count', [19, 20])
def test_delivery_hour_uses_real_remember_rounds_and_minimum_twenty(nudges, count):
    nudges.env.model.allowed = False
    _dated_document(nudges.env, '2026年10月10日预约复诊')
    _remember_times(nudges, count, 17)
    first = asyncio.run(nudges.owner.deliver('alpha'))
    assert len(first) == (1 if count < 20 else 0)
    nudges.clock.at = nudges.clock.at.astimezone(LOCAL).replace(hour=17, minute=30).astimezone(UTC)
    assert len(asyncio.run(nudges.owner.deliver('alpha'))) == 1


@pytest.mark.parametrize('setting', ['disabled', 'cleared'])
def test_delivery_hour_respects_actual_signal_disabled_and_clear_cutoff(nudges, setting):
    nudges.env.model.allowed = False
    _dated_document(nudges.env, '2026年10月10日预约复诊')
    _remember_times(nudges, 20, 17)
    assert asyncio.run(nudges.owner.deliver('alpha')) == []
    if setting == 'disabled':
        nudges.signals.set_enabled(False, expected_revision=0)
    else:
        nudges.signals.clear(expected_revision=0)
    assert len(asyncio.run(nudges.owner.deliver('alpha'))) == 1
    assert nudges.env.model.calls == 0


def test_delivery_hour_keeps_weekday_and_weekend_rounds_separate(nudges):
    nudges.clock.at = datetime(2026, 10, 10, 2, tzinfo=UTC)
    nudges.env.model.allowed = False
    _dated_document(nudges.env, '2026年10月11日预约复诊')
    _remember_times(nudges, 20, 17)
    _remember_times(nudges, 20, 14, weekend=True)
    assert asyncio.run(nudges.owner.deliver('alpha')) == []
    nudges.clock.at += timedelta(hours=4, minutes=30)
    assert len(asyncio.run(nudges.owner.deliver('alpha'))) == 1


@pytest.mark.parametrize('change', ['private_after_wire', 'archive_after_wire'])
def test_nudge_consumer_keeps_real_source_and_privacy_checks_after_model(nudges, change):
    env = nudges.env
    identity = _dated_document(env, '2026年10月10日预约复诊')
    if change == 'private_after_wire':
        env.model.after = lambda: set_private_project(env.records, 'alpha', True, 0)
    else:
        env.model.after = lambda: env.documents.archive(identity, expected_revision=1)
    with pytest.raises(HTTPException) as failure:
        asyncio.run(nudges.owner.deliver('alpha'))
    assert failure.value.status_code == 409
    assert env.model.calls == 1
    assert env.records.list('workspace_ask_receipts')
    assert all(row.payload['status'] == 'failed' for row in env.records.list('workspace_ask_receipts'))
    assert all(row.payload['state'] == 'failed' for row in env.records.list('v2_nudges'))


def test_rebuilt_user_reminder_projection_uses_actual_changed_time_and_state(nudges):
    nudges.clock.at = datetime(2026, 10, 8, 0, tzinfo=UTC)
    fact = nudges.reminders.create(project_id='alpha', scene=None, text='提醒我今天早上九点喝水', turn_id='turn-change')
    nudges.clock.at += timedelta(hours=2)
    first = asyncio.run(nudges.owner.deliver('alpha'))
    changed = nudges.reminders.update(fact['id'], project_id='alpha', at='2026-10-08T12:00:00+08:00', expected_revision=fact['revision'])
    assert asyncio.run(nudges.owner.deliver('alpha')) == []
    nudges.clock.at += timedelta(hours=3)
    rebuilt = asyncio.run(nudges.owner.deliver('alpha'))
    assert len(rebuilt) == 1 and rebuilt[0]['id'] == first[0]['id']
    assert rebuilt[0]['at'] == changed['at']
    nudges.reminders.update(fact['id'], project_id='alpha', state='deleted', expected_revision=changed['revision'])
    assert asyncio.run(nudges.owner.deliver('alpha')) == []
    assert nudges.env.records.read('v2_reminders', fact['id']).payload['state'] == 'deleted'


def test_fc_total_budget(tmp_path):
    """create_app 与真实配置、网关和内核；仅外层 provider completion 函数为合成边界。"""
    from pathlib import Path
    from uuid import uuid4
    from fastapi.testclient import TestClient
    from backend.memory_app.app import create_app
    from backend.memory_app.model_config import ModelConfiguration
    from backend.memory_app.storage_authority import resolve_recognition_document_store
    from backend.memory_app.v2.policies import get
    from backend.security.secrets import InMemorySecretStore
    root = tmp_path.parent / ('fc-' + uuid4().hex[:8])
    root.mkdir()
    (root / 'config').mkdir()
    (root / 'config' / 'settings.toml').write_bytes(
        (Path(__file__).parents[3] / 'config' / 'settings.toml.example').read_bytes())
    records, _ = resolve_recognition_document_store(root)
    calls = []
    def provider(**request):
        calls.append({'messages': deepcopy(request['messages'])})
        numbers = [int(n) for n in re.findall(r'^\[(\d+)\]', request['messages'][-1]['content'], re.MULTILINE)]
        content = json.dumps({'answer': 'Synthetic bounded answer', 'citations': numbers})
        return {'choices': [{'message': {'content': content}, 'finish_reason': 'stop'}],
                'usage': {'prompt_tokens': 7, 'completion_tokens': 3}}
    models = ModelConfiguration(records, root, InMemorySecretStore(), completion_fn=provider)
    models.update('generation', {'base_url': 'https://example.test/v1', 'model': 'test-model',
        'api_key': 'synthetic-only', 'allow_remote': True, 'expected_revision': 0})
    app = create_app(runtime_root=root, model_configuration=models)
    with TestClient(app) as http:
        full = SimpleNamespace(root=root, records=records, model=models, http=http,
            service=app.state.recognition_service, domains=app.state.workspace_domains,
            documents=app.state.workspace_domains.query.documents)
        for text in ('alpha 固定预算先核对账单', 'beta 医院预约核对联系人', 'gamma 出门记得带雨伞'):
            publish(full, text=text)
        for text in ('我习惯看纸质日历安排预约', '我出门喜欢携带蓝色水杯'):
            publish(full, text=text, project='me')
        policy = get('nudge', version='@2')
        query, question = full.domains.query, '具体 alpha beta gamma 原文数据'
        plan = query.prepare_ask('alpha', question, evidence_limit=policy.evidence_limit,
                                 profile_limit=policy.profile_limit)
        assert len(plan['chosen']) == 2 and len(plan['profile']['items']) == 1
        assert len(plan['profile']['basis']['items']) == 2
        frozen = deepcopy(plan['chosen'])
        result, receipt, request = _execute(full, plan)
        assert len(calls) == 1
        assert len(receipt.payload['sources']) == 3 and len(result['sources']) == 2
        assert {source['id'] for source in result['sources']} == {source['id'] for source in frozen}
        assert calls[0]['messages'][0]['content'].startswith(PREFIX)
        assert calls[0]['messages'][0]['content'].count('\n- ') == 1
        assert len(re.findall(r'^\[\d+\]', calls[0]['messages'][-1]['content'], re.MULTILINE)) == 2
        assert request['execution_policy']['template_version'] == 2


@pytest.mark.parametrize('city,identity', [
    ('纽约', 5128581), ('上海', 1796236), ('东京', 1850147),
    ('Barcelona', 3128760), ('成都', 1815286),
])
def test_real_city_resource_recognizes_ordinary_city_and_administrative_seats(nudges, city, identity):
    _dated_document(nudges.env, '准备行李和车票', scene='旅行/' + city)
    candidates = nudges.owner.place_candidates('alpha')
    assert any(row['city_id'] == identity and row['city'] == city for row in candidates)


@pytest.mark.parametrize('word', ['美食', '旅行', '预约', '生日'])
def test_generic_topics_are_not_converted_into_city_candidates(nudges, word):
    _dated_document(nudges.env, word, scene=word)
    assert nudges.owner.place_candidates('alpha') == []


def test_city_alias_with_multiple_geonames_ids_is_rejected_without_guessing(nudges):
    from backend.memory_app.v2.nudges import _cities
    resource = _cities()
    ambiguous = next(name for name, cities in resource['names'].items() if len(cities) > 1)
    from backend.memory_app.v2.policies import get
    assert get('nudge', version='@2').city_mentions('', ambiguous, resource) == []


def test_real_city_delivery_uses_local_match_and_day_cap_with_date_candidates(nudges):
    nudges.env.model.allowed = False
    document = _dated_document(nudges.env, '准备行李和车票', scene='旅行/纽约')
    _dated_document(nudges.env, '2026年10月9日预约复诊')
    _dated_document(nudges.env, '2026年10月10日预约复诊')
    wrong = asyncio.run(nudges.owner.deliver('alpha', city='成都'))
    assert all(row['kind'] != 'place' for row in wrong)
    nudges.clock.at += timedelta(days=1)
    reached = asyncio.run(nudges.owner.deliver('alpha', city='纽约'))
    assert len(reached) <= 2
    assert any(row['kind'] == 'place' and row['source_id'] == document for row in reached)
    assert nudges.env.model.calls == 0


def test_confirmed_recognition_birthday_reaches_template_finish_with_original_source_guards(nudges):
    from backend.memory_app.original_sources import document_roots, original
    env, scope = nudges.env, WorkScope('local-user', 'alpha')
    text = '小王生日是1990年10月10日'
    document_id = _dated_document(env, text)
    birthday, experience_id = publish(env, text=text, doc=document_id)
    document = deepcopy(env.documents.read(document_id))
    original_markdown = env.documents.markdown(document_id, revision=document['revision'])
    recognition = env.records.read('recognitions', birthday.id)
    assert recognition.payload['published_by'] == 'local-user'
    assert recognition.revision == birthday.revision and recognition.payload['state'] == 'active'
    experience = env.records.read('recognition_experiences', experience_id)
    assert experience.payload['content'] == original_markdown.strip()
    assert experience.payload['provenance']['source_refs'] == [
        {'type': 'document', 'id': document_id, 'revision': document['revision']}]
    qualified = env.service.get_recognition(scope=scope, recognition_id=birthday.id)
    assert qualified.id == birthday.id and qualified.scope == scope
    assert qualified.authorized and qualified.source_evidence_complete and qualified.source_evidence
    assert qualified.source_experience_ids == (experience_id,)
    assert qualified.source_experience_revisions[experience_id] == experience.revision
    owner_id = next(ref['source_id'] for ref in document['source_refs']
                    if ref['locator'] == 'workspace://' + ref['source_id'])
    original_owner = env.records.read('workspace_items', owner_id)
    assert original_owner.payload['status'] == 'confirmed'
    assert original_owner.payload['project_id'] == 'alpha'
    assert original_owner.payload['document_id'] == document_id
    originals = {}
    for kind, source_id, revision in document_roots(env.records, scope, document['source_refs']):
        if kind == 'original_source':
            source = original(env.records, scope, kind, source_id)
            assert source.revision == revision
            originals[(kind, source_id)] = (deepcopy(source.payload), source.revision)
    assert originals
    fact_collections = ('recognitions', 'recognition_candidates', 'recognition_experiences',
                        'documents', 'document_revisions', 'document_markdown', 'workspace_items')
    facts = [(name, row.object_id, row.revision, deepcopy(row.payload))
             for name in fact_collections for row in env.records.list(name)]
    prepared = next(source for source in nudges.owner._sources('alpha')
                    if source['source_kind'] == 'recognition' and source['source_id'] == birthday.id)
    assert prepared['_guard'] == ('recognitions', birthday.id, recognition.revision, recognition.payload)
    env.model.allowed = False
    dates = nudges.owner.refresh('alpha')
    assert len(dates) == 1 and dates[0]['source_kind'] == 'recognition'
    assert dates[0]['source_id'] == birthday.id and dates[0]['source_revision'] == birthday.revision
    assert dates[0]['annual'] is True
    assert datetime.fromisoformat(dates[0]['at']).astimezone(LOCAL).date().isoformat() == '2026-10-10'
    delivered = asyncio.run(nudges.owner.deliver('alpha'))
    assert len(delivered) == 1
    item = delivered[0]
    assert item['kind'] == 'date' and item['source_kind'] == 'recognition'
    assert item['source_id'] == birthday.id and item['source_revision'] == birthday.revision
    assert item['state'] == 'delivered' and item['revision'] == 2
    assert item['text'] == '2天后：' + text and item['evidence_ids'] == [birthday.id]
    assert item['model_used'] is False and item['egress_receipt_id'] is None
    public = nudges.owner.list('alpha')
    assert public == delivered
    stored_dates, stored_nudges = env.records.list('v2_dates'), env.records.list('v2_nudges')
    assert len(stored_dates) == 1 and len(stored_nudges) == 1
    assert stored_nudges[0].revision == 2 and stored_nudges[0].payload['state'] == 'delivered'
    guard_keys = {'_guard', '_date_guard', '_source_input', '_source_text'}
    pending = [dates, delivered, public,
               [row.payload for row in stored_dates], [row.payload for row in stored_nudges]]
    while pending:
        value = pending.pop()
        if isinstance(value, dict):
            assert guard_keys.isdisjoint(value)
            pending.extend(value.values())
        elif isinstance(value, (list, tuple)):
            pending.extend(value)
    assert [(name, row.object_id, row.revision, row.payload)
            for name in fact_collections for row in env.records.list(name)] == facts
    assert env.documents.read(document_id) == document
    assert env.documents.markdown(document_id, revision=document['revision']) == original_markdown
    assert env.records.read('workspace_items', owner_id) == original_owner
    for (kind, source_id), (payload, revision) in originals.items():
        current = original(env.records, scope, kind, source_id)
        assert current.payload == payload and current.revision == revision
    current = env.service.get_recognition(scope=scope, recognition_id=birthday.id)
    assert current.id == qualified.id and current.revision == qualified.revision
    assert current.authorized and current.source_evidence_complete
    assert env.records.list('workspace_ask_receipts') == () and env.model.calls == 0
