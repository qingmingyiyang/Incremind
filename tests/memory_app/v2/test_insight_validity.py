"""Confirmation facts and historical recall use real temporary domain records."""
from unittest.mock import patch
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest

from backend.memory_app.v2.links import InsightLinks
from backend.memory_app.v2.policies import get, override
from backend.memory_app.v2.budget import source_texts, ask_instruction, input_tokens
from backend.memory_app.source_egress import SourceEgressService
from backend.memory_app.recall_preferences import set_preference
from backend.recognition import WorkScope, RecognitionConflict
from core.storage_provider import SQLiteUnitOfWorkConflict
from tests.memory_app.v2.test_ladder import env as _env, recognition

env = _env
MARCH = '2026-03-12T00:00:00+00:00'
JUNE = '2026-06-12T00:00:00+00:00'
SEPTEMBER = '2026-09-12T00:00:00+00:00'
TODAY = '2026-10-03T00:00:00+00:00'


@pytest.fixture(autouse=True)
def configured_local_model(env):
    env.query.models = SimpleNamespace(public=lambda: {})


def publish(env, body, at=MARCH, project='alpha'):
    with patch('backend.recognition.service._now', return_value=at):
        return recognition(env, body, project=project)


def supersede(env, old, new, at=JUNE):
    links = InsightLinks(env.records, env.service)
    proposal = links.propose('alpha', new.id, old.id, 'supersedes', 'Synthetic user confirmation')
    with patch('backend.memory_app.v2.links.now', return_value=at):
        links.review('alpha', proposal['id'], proposal['revision'], True)


def test_publish_validity_is_confirmation_time_and_leaves_source_payload(env):
    old = publish(env, '春港展会在北厅办小型展')
    row = env.records.read('recognitions', old.id)
    marker = env.records.read('v2_insight_validity', old.id)
    assert marker is not None
    assert marker.payload == {'valid_from': MARCH, 'valid_until': None, 'superseded_by': None}
    assert row.revision == 1
    assert env.records.read('recognition_versions', old.id + '~v1').payload['snapshot'] == row.payload
    assert 'valid_from' not in row.payload


def test_validity_conflict_rolls_back_publication_and_version(env):
    scope = WorkScope('local-user', 'alpha')
    experience = env.service.stage_experience(scope=scope, content='Synthetic evidence')
    candidate = env.service.propose(scope=scope, content='待确认认识', source_experience_ids=[experience])
    with env.records.begin() as tx:
        tx.put('v2_insight_validity', 'occupied', {'valid_from': MARCH, 'valid_until': None,
            'superseded_by': None}, expected_revision=0)
        tx.commit()
    with pytest.raises(SQLiteUnitOfWorkConflict, match='expected revision 0, found 1'):
        env.service.publish(scope=scope, candidate_id=candidate.id, expected_revision=1,
                            reviewer='local-user', recognition_id='occupied')
    assert env.records.read('recognitions', 'occupied') is None
    assert env.records.read('recognition_versions', 'occupied~v1') is None
    assert env.records.read('recognition_candidates', candidate.id).payload['state'] == 'pending'


def test_supersede_closes_old_interval_in_same_review_transaction(env):
    old = publish(env, '春港展会在北厅办小型展')
    new = publish(env, '春港展会改在南厅办大型展', JUNE)
    before = env.records.read('recognitions', old.id)
    supersede(env, old, new)
    assert env.records.read('v2_insight_validity', old.id).payload == {
        'valid_from': MARCH, 'valid_until': JUNE, 'superseded_by': new.id}
    assert env.records.read('recognitions', old.id) == before


def test_historical_recall_filters_future_and_marks_actual_source_framing(env):
    old = publish(env, '春港展会在北厅办小型展')
    new = publish(env, '春港展会改在南厅办大型展', JUNE)
    supersede(env, old, new)
    with override(retrieve='@3', compose='@3'):
        plan = env.query.prepare_ask('alpha', '春港展会在2026年三月时怎么想的？')
        assert {c['id'] for c in plan['chosen']} == {old.id}
        assert plan['chosen'][0]['layer'] == 'L3'
        assert '当时' in source_texts(plan['chosen'])[0]
        assert '2026-03-12' in source_texts(plan['chosen'])[0]
        assert '当时' in ask_instruction(plan['chosen'])
        assert input_tokens(plan['chosen'], plan['question']) <= plan['budget']


def test_month_range_and_event_question_recall_both_then_valid_insights(env):
    one = publish(env, '海棠巡检计划每月巡检')
    two = publish(env, '海棠巡检开始每周巡检', JUNE)
    three = publish(env, '海棠巡检改为每日巡检', SEPTEMBER)
    supersede(env, one, two)
    supersede(env, two, three, SEPTEMBER)
    with override(retrieve='@3', compose='@3'):
        plan = env.query.prepare_ask('alpha', '海棠巡检从2026年三月到六月先后做了什么？')
        assert {c['id'] for c in plan['chosen']} == {one.id, two.id}
        event = env.query.prepare_ask('alpha', '海棠巡检什么时候开始每周巡检？')
        assert {c['id'] for c in event['chosen']} == {two.id}


def test_undated_selection_trace_and_prompt_bytes_remain_identical(env):
    old = publish(env, '春港展会在北厅办小型展')
    new = publish(env, '春港展会改在南厅办大型展', JUNE)
    supersede(env, old, new)
    rows = []
    for version in ('@2', '@3'):
        with override(retrieve=version, compose=version):
            plan = env.query.prepare_ask('alpha', '春港展会怎么安排？')
            rows.append(([c['id'] for c in plan['chosen']], plan['trace'], source_texts(plan['chosen']),
                         ask_instruction(plan['chosen']), input_tokens(plan['chosen'], plan['question'])))
    assert rows[0] == rows[1]


@pytest.mark.parametrize('blocked', ['private', 'forgotten', 'revoked', 'project'])
def test_past_validity_never_bypasses_current_source_boundaries(env, blocked):
    old = publish(env, '春港展会在北厅办小型展')
    scope = WorkScope('local-user', 'alpha')
    if blocked == 'private':
        SourceEgressService(env.records).set_policy(scope, 'recognition', old.id, 1, 0, [])
    elif blocked == 'forgotten':
        set_preference(env.records, scope, old.id, recognition_revision=1, preference_revision=0, state='forgotten')
    elif blocked == 'revoked':
        env.service.revoke(scope=scope, recognition_id=old.id, expected_revision=1, reason='user')
    with override(retrieve='@3', compose='@3'):
        plan = env.query.prepare_ask('other' if blocked == 'project' else 'alpha',
                                    '春港展会在2026年三月时怎么想的？')
    assert plan['chosen'] == []


@pytest.mark.parametrize('question, start, end', [
    ('三月时怎么想的？', '2026-03-01', '2026-04-01'),
    ('2025年12月当时怎么想的？', '2025-12-01', '2026-01-01'),
    ('上周怎么认为？', '2026-09-21', '2026-09-28'),
    ('从三月到六月怎么变化？', '2026-03-01', '2026-07-01'),
])
def test_pure_time_parser_uses_explicit_reference_clock(question, start, end):
    result = get('retrieve', version='@3')(None, question, TODAY, operation='time')
    zone = timezone(timedelta(hours=8))
    assert datetime.fromisoformat(result['start']).astimezone(zone).date().isoformat() == start
    assert datetime.fromisoformat(result['end']).astimezone(zone).date().isoformat() == end


def test_current_question_uses_frozen_reference_and_excludes_superseded(env):
    old = publish(env, '晨桥冷库恒温设为四摄氏度')
    new = publish(env, '晨桥冷库恒温设为二摄氏度', JUNE)
    supersede(env, old, new)
    with patch('backend.memory_app.v2.insight_validity.now', return_value=TODAY), override(retrieve='@3', compose='@3'):
        plan = env.query.prepare_ask('alpha', '晨桥冷库现在采用什么决定？')
        assert {c['id'] for c in plan['chosen']} == {new.id}
        assert plan['chosen'][0]['time_scope']['reference'] == TODAY
    env.query.validate_ask_plan(plan)


def test_backfill_temporary_history_twice_is_idempotent_and_keeps_all_original_records(env):
    from backend.memory_app.v2.insight_validity import backfill, read_validity
    old = publish(env, '春港展会在北厅办小型展')
    new = publish(env, '春港展会改在南厅办大型展', JUNE)
    later = publish(env, '春港展会改在东厅', SEPTEMBER)
    supersede(env, old, new)
    supersede(env, old, later, SEPTEMBER)
    with env.records.begin() as tx:
        for row in tx.list('v2_insight_validity'):
            tx.delete('v2_insight_validity', row.object_id, expected_revision=row.revision)
        tx.commit()
    collections = ['recognitions', 'recognition_candidates', 'recognition_versions',
                   'recognition_relations', 'v2_insight_interference', 'recognition_recall_preferences']
    before = {name: env.records.list(name) for name in collections}
    assert read_validity(env.records, old.id) == {'valid_from': MARCH, 'valid_until': JUNE, 'superseded_by': new.id}
    assert backfill(env.records) == {'added': 3, 'total': 3}
    first = env.records.list('v2_insight_validity')
    assert backfill(env.records) == {'added': 0, 'total': 3}
    assert env.records.list('v2_insight_validity') == first
    assert {name: env.records.list(name) for name in collections} == before


def test_existing_persona_relation_remains_allowed(env):
    old = publish(env, '我倾向安排小型展', project='me')
    new = publish(env, '春港展会安排大型展', JUNE)
    supersede(env, old, new)
    assert env.records.read('v2_insight_validity', old.id).payload['superseded_by'] == new.id


def test_supersession_after_preview_invalidates_frozen_validity_but_never_reinterprets_clock(env):
    old = publish(env, '春港展会在北厅办小型展')
    with patch('backend.memory_app.v2.insight_validity.now', return_value=TODAY), override(retrieve='@3', compose='@3'):
        plan = env.query.prepare_ask('alpha', '春港展会现在采用什么决定？')
    new = publish(env, '春港展会安排大型展', TODAY)
    supersede(env, old, new, TODAY)
    with pytest.raises(RecognitionConflict, match='validity changed'):
        env.query.validate_ask_plan(plan)


def test_condensed_question_cannot_drop_original_date_scope(env):
    old = publish(env, '春港展会在北厅办小型展')
    new = publish(env, '春港展会改在南厅办大型展', JUNE)
    supersede(env, old, new)
    with override(retrieve='@3', compose='@3'):
        collected = env.query.collect_candidates('alpha', '春港展会安排')
        plan = env.query.prepare_ask('alpha', '春港展会在2026年三月时怎么想的？',
                                    retrieval_question='春港展会安排', collected=collected)
    assert {c['id'] for c in plan['chosen']} == {old.id}
    assert plan['time_scope']['mode'] == 'interval'


def test_time_range_preserves_distinct_validity_intervals_for_similar_statements(env):
    body = '春港展会采用统一流程核实场地容量活动路线参会名单接待时间和安全准备，'
    old = publish(env, body + '安排在北厅')
    new = publish(env, body + '安排在南厅', JUNE)
    supersede(env, old, new)
    with override(retrieve='@3', compose='@3'):
        plan = env.query.prepare_ask('alpha', '春港展会从三月到六月先后怎么安排？')
    assert {c['id'] for c in plan['chosen']} == {old.id, new.id}


@pytest.mark.parametrize('created_at, exists', [('2026-11-01T00:00:00+00:00', False), (TODAY, True)])
def test_current_reference_respects_future_and_exact_point_for_unlinked_evidence(tmp_path, created_at, exists):
    from tools.memory_eval import seed
    fixture = {'documents': [
        {'id': 'future', 'source_id': 'future-source', 'title': '晨桥冷库未来决定', 'project_id': 'alpha',
         'created_at': created_at, 'summary': '晨桥冷库恒温设为零摄氏度',
         'body': '晨桥冷库具体原话是恒温设为零摄氏度', 'original': '晨桥冷库具体原话是恒温设为零摄氏度'}],
        'insights': [], 'questions': []}
    query, identities = seed(tmp_path, fixture)
    question = '晨桥冷库现在采用什么决定具体原话？'
    with patch('backend.memory_app.v2.insight_validity.now', return_value=TODAY), override(retrieve='@3', compose='@3'):
        collected = query.collect_candidates('alpha', question)
        plan = query.prepare_ask('alpha', question, collected=collected)
    assert (identities['future'] in {c['id'] for c in collected['candidates']}) is exists
    assert ('future-source' in {c['id'] for c in collected['candidates']}) is exists
    assert (identities['future'] in {c['id'] for c in plan['chosen']}) is exists
    if not exists:
        assert plan['chosen'] == []


def test_validity_failure_rolls_back_relation_review_edge_and_interference(env):
    old = publish(env, '春港展会在北厅办小型展')
    new = publish(env, '春港展会改在南厅办大型展', JUNE)
    links = InsightLinks(env.records, env.service)
    proposal = links.propose('alpha', new.id, old.id, 'supersedes', 'Synthetic user confirmation')
    original = env.records.read('v2_insight_validity', old.id)
    with sqlite3.connect(env.records.database_path) as conn:
        conn.execute("CREATE TRIGGER validity_failure BEFORE UPDATE ON crp_structured_records "
            "WHEN OLD.collection = 'v2_insight_validity' BEGIN SELECT RAISE(ABORT, 'synthetic validity failure'); END")
    with pytest.raises(sqlite3.IntegrityError, match='synthetic validity failure'):
        with patch('backend.memory_app.v2.links.now', return_value=JUNE):
            links.review('alpha', proposal['id'], proposal['revision'], True)
    assert env.records.read('recognition_relation_proposals', proposal['id']).payload['state'] == 'pending'
    assert env.records.list('recognition_relations') == ()
    assert env.records.read('v2_insight_interference', old.id) is None
    assert env.records.read('recognition_recall_preferences', old.id) is None
    assert env.records.read('v2_insight_validity', old.id) == original


def test_backfill_cli_twice_keeps_exact_revisions_and_original_facts(env):
    old = publish(env, '春港展会在北厅办小型展')
    new = publish(env, '春港展会改在南厅办大型展', JUNE)
    supersede(env, old, new)
    with env.records.begin() as tx:
        for row in tx.list('v2_insight_validity'):
            tx.delete('v2_insight_validity', row.object_id, expected_revision=row.revision)
        tx.commit()
    before = env.records.list('recognitions')
    script = Path(__file__).resolve().parents[3] / 'tools/backfill_insight_validity.py'
    first = subprocess.run([sys.executable, str(script), '--database', str(env.records.database_path)],
                           capture_output=True, text=True, check=True)
    assert first.stdout.strip() == '{"added": 2, "total": 2}'
    marked = env.records.list('v2_insight_validity')
    second = subprocess.run([sys.executable, str(script), '--database', str(env.records.database_path)],
                            capture_output=True, text=True, check=True)
    assert second.stdout.strip() == '{"added": 0, "total": 2}'
    assert env.records.list('v2_insight_validity') == marked
    assert env.records.list('recognitions') == before


def test_unspecified_past_also_keeps_a_still_valid_earlier_confirmation(env):
    old = publish(env, '春港展会在北厅办小型展')
    with patch('backend.memory_app.v2.insight_validity.now', return_value=TODAY), override(retrieve='@3', compose='@3'):
        plan = env.query.prepare_ask('alpha', '春港展会以前怎么想的？')
    assert {c['id'] for c in plan['chosen']} == {old.id}
