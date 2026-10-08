import json
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from backend.recognition import RecognitionService, WorkScope
from backend.memory_app.v2.signals import SignalService
from backend.memory_app.v2.usage import UsageService
from core.storage_provider import SQLiteStructuredRecordStore


@pytest.fixture
def env(tmp_path):
    records = SQLiteStructuredRecordStore(tmp_path / 'records.sqlite3')
    service = RecognitionService(records)
    scope = WorkScope('local-user', 'alpha')
    experience = service.stage_experience(scope=scope, content='Synthetic private evidence')
    candidate = service.propose(scope=scope, content='Synthetic private recognition',
        source_experience_ids=[experience])
    clock = [datetime(2026, 10, 6, 12, tzinfo=timezone.utc)]
    return SimpleNamespace(records=records, service=service, scope=scope, candidate=candidate,
        clock=clock, usage=UsageService(records, now=lambda: clock[0]),
        signals=SignalService(records, now=lambda: clock[0]))


def published(env):
    return env.service.publish(scope=env.scope, candidate_id=env.candidate.id,
        expected_revision=1, reviewer='local-user')


def open_insight(env, insight):
    return env.usage.record_usage('insight', insight.id, 'alpha', 1.0, event_kind='open')


def test_published_insight_open_is_counted_without_changing_recognition(env):
    insight = published(env)
    before = env.records.read('recognitions', insight.id)
    result = open_insight(env, insight)
    usage = env.records.read('v2_usage_insight', insight.id)
    assert result['events'][-1] == {'kind': 'open', 'at': env.clock[0].isoformat()}
    assert usage.payload == result
    report = env.signals.report()
    assert report['after_answer']['opens'] == 1
    assert report['after_answer']['unlocated_uses'] == 0
    assert set(report) == {'unused', 'corrections', 'reask', 'after_answer', 'dwell',
        'document_edits', 'interruptions'}
    assert 'Synthetic private' not in json.dumps(report)
    assert env.records.read('recognitions', insight.id) == before
    assert env.records.read('v2_usage_insight', insight.id) == usage


def test_insight_open_obeys_off_clear_and_exact_cutoff(env):
    insight = published(env)
    open_insight(env, insight)
    assert env.signals.report()['after_answer']['opens'] == 1
    state = env.signals.set_enabled(False, expected_revision=0)
    assert env.signals.report() == {}
    state = env.signals.set_enabled(True, expected_revision=state['revision'])
    env.signals.clear(expected_revision=state['revision'])
    # Usage history remains owned by the original service; cutoff filters it.
    original = env.records.read('v2_usage_insight', insight.id)
    assert original.payload['events'][-1]['at'] == env.clock[0].isoformat()
    assert env.signals.report()['after_answer']['opens'] == 0
    open_insight(env, insight)
    assert env.signals.report()['after_answer']['opens'] == 0
    env.clock[0] += timedelta(seconds=1)
    open_insight(env, insight)
    assert env.signals.report()['after_answer']['opens'] == 1


def test_pending_insight_open_does_not_create_usage(env):
    assert env.usage.record_usage('insight', env.candidate.id, 'alpha', 1.0,
        event_kind='open') is None
    assert env.records.list('v2_usage_insight') == ()
    assert env.signals.report()['after_answer']['opens'] == 0
