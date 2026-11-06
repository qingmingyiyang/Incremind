import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from backend.memory_app.v2.policies import ACTIVE, get
from core.storage_provider import SQLiteStructuredRecordStore


def test_quick_correction_uses_only_the_latest_eligible_outcome(tmp_path):
    from backend.memory_app.v2.workbench import _quick_outcome

    records = SQLiteStructuredRecordStore(tmp_path / 'quick-outcome.sqlite3')
    now = datetime(2026, 11, 6, tzinfo=timezone.utc)
    receipt = {'state': 'done', 'document_id': 'outcome-a'}
    previous = {'project_id': 'project-a', 'thread_id': 'thread-a', 'intent': 'do',
                'created_at': now.isoformat(), 'updated_at': now.isoformat(), 'receipt': {'do': receipt}}
    with records.begin() as tx:
        tx.put('v2_turns', 'turn-a', previous, expected_revision=0)
        tx.put('v2_turns', 'turn-other', {**previous, 'thread_id': 'thread-other'}, expected_revision=0)
        tx.commit()

    def target(text='不对，改成表格', *, elapsed=600, tag=None, previous_id='turn-a', **overrides):
        body = {'thread_id': 'thread-a', 'text': text, **overrides}
        return _quick_outcome(records, body, project='project-a', tag=tag, text=text,
                              accepted_at=(now + timedelta(seconds=elapsed)).isoformat(),
                              acceptance={'previous_id': previous_id})

    assert target() == 'outcome-a'
    assert target('另外补充一个例子', intent='auto') == 'outcome-a'
    assert target(previous_id=None) == 'outcome-a'
    for options in ({'elapsed': 601}, {'elapsed': -1}, {'tag': '其他项目'}, {'item_ids': ['attachment']},
                    {'continue_from': None}, {'executor': 'codex'}, {'previous_id': 'turn-pending'},
                    {'previous_id': 'turn-other'}, {'text': '不对，更新热词表'}, {'text': '今天有什么安排'}):
        assert target(**options) is None
    with records.begin() as tx:
        tx.put('v2_turns', 'turn-a', {**previous, 'receipt': {'do': {**receipt, 'executor': 'codex'}}},
               expected_revision=1)
        tx.commit()
    assert target() is None


def test_explicit_policy_registry_and_labeled_synthetic_cases():
    fixture = Path(__file__).parents[2] / 'fixtures/steer/cases.json'
    cases = json.loads(fixture.read_text(encoding='utf-8'))
    assert cases['synthetic'] is True
    assert len(cases['cases']) >= 40
    policy = get('steer', version='@1')
    for case in cases['cases']:
        assert policy(case['text'])['category'] == case['label'], case['id']
    assert ACTIVE['steer'] == '@2'
    assert ACTIVE['consolidate'] == '@3'
    assert get('steer') is get('steer', version='@2')
    assert 'stale' not in ACTIVE


def test_unsupported_external_steer_respects_the_public_project_boundary(tmp_path):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from backend.memory_app.v2.steer import SteerService, install_steer_routes
    from core.document_engine import SQLiteDocumentRepository

    records = SQLiteStructuredRecordStore(tmp_path / 'steer.sqlite3')
    application = FastAPI()
    application.state.recognition_documents = SQLiteDocumentRepository(records, namespace_id='recognition')
    service = SteerService(application=application, records=records, runtime_root=tmp_path)
    install_steer_routes(application, service=service)
    with records.begin() as tx:
        for executor in ('codex', 'claude-code'):
            tx.put('v2_turns', executor, {'project_id': 'alpha', 'intent': 'do',
                   'receipt': {'do': {'state': 'running', 'executor': executor}}}, expected_revision=0)
        tx.commit()
    with TestClient(application) as client:
        for executor in ('codex', 'claude-code'):
            path = f'/api/v2/workbench/turns/{executor}/steer'
            headers = {'Idempotency-Key': 'sa3-unsupported'}
            denied = client.post(path, json={'project_id': 'beta', 'text': '补充'}, headers=headers)
            assert denied.status_code == 404
            result = client.post(path, json={'project_id': 'alpha', 'text': '补充'}, headers=headers)
            assert result.status_code == 409 and result.json() == {'detail': 'steer_unsupported'}
            assert client.get(path, params={'project_id': 'alpha'}).json()['steers'] == []


def test_steer_quota_characters_followup_and_visible_tail():
    policy = get('steer', version='@1')
    assert policy.validate('字' * 2000, count=4) == '字' * 2000
    for text, count in [('字' * 2001, 0), ('', 0), ('修改', 5)]:
        with pytest.raises(ValueError):
            policy.validate(text, count=count)
    assert policy.followup('2026-11-03T00:00:00+00:00', '2026-11-03T00:10:00+00:00')
    assert not policy.followup('2026-11-03T00:00:00+00:00', '2026-11-03T00:10:01+00:00')
    assert not policy.followup('2026-11-03T00:00:00+00:00', '2026-11-02T23:59:59+00:00')
    assert policy.tail('已显示' * 101) == ('已显示' * 101)[-300:]
    assert policy('以后都用表格')['direction'] != policy('以后都不要用表格')['direction']


def test_stale_ignores_review_and_automatic_memory_state():
    policy = get('stale', version='@1')
    frozen = {'kind': 'insight', 'id': 'i1', 'project_id': 'p1', 'revision': 1, 'content': '原文'}
    current = {**frozen, 'available': True, 'revision': 2, 'reviewed': True,
               'strength': 0.2, 'auto_forgotten': True}
    assert policy(frozen,current,reference='2026-11-03T00:00:00+00:00')['reasons'] == []
    current.update(content='修改正文',manual_forgotten=True)
    assert policy(frozen,current,reference='2026-11-03T00:00:00+00:00')['reasons'] == ['edited','forgotten']
    assert policy(frozen,None,reference='2026-11-03T00:00:00+00:00')['verified'] is False
