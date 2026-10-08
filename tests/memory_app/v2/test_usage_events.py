"""Actual SQLite usage facts, citation-only admission and bounded event history."""
from datetime import datetime, timedelta, timezone

from backend.memory_app.v2.usage import UsageService, record_answer_usage
from tests.memory_app.v2.test_workbench_ask import env, publish, add_document


def test_unquoted_context_does_not_create_or_change_usage(env):
    cited, _ = publish(env, 'citation evidence')
    other, _ = publish(env, 'unquoted evidence')
    doc, _ = add_document(env)
    original = env.records.read('recognitions', other.id)
    chosen = [{'layer': 'L3', 'entry': {'id': cited.id}},
              {'layer': 'L3', 'entry': {'id': other.id}},
              {'layer': 'L2', 'entry': {'document_id': doc}}]
    record_answer_usage(env.records, chosen, [{'n': 1}], 'alpha')
    assert env.records.read('v2_usage_insight', other.id) is None
    assert env.records.read('v2_usage_document', doc) is None
    assert env.records.read('recognitions', other.id) == original
    row = env.records.read('v2_usage_insight', cited.id)
    assert row.payload['count'] == 2
    assert row.payload['events'][-1]['kind'] == 'citation'


def test_usage_initialization_and_repetition_are_exact_facts(env):
    insight, _ = publish(env)
    now = datetime(2026, 10, 5, tzinfo=timezone.utc)
    tracker = UsageService(env.records, now=lambda: now)
    tracker.initialize('insight', insight.id, 'alpha')
    row = env.records.read('v2_usage_insight', insight.id)
    assert row.payload['events'] == [{'at': now.isoformat(), 'kind': 'initialize'}]
    assert row.payload['older_count'] == 0
    tracker.initialize('insight', insight.id, 'alpha')
    assert env.records.read('v2_usage_insight', insight.id) == row


def test_latest_64_usage_events_preserve_count_and_older_count(env):
    insight, _ = publish(env)
    now = [datetime(2026, 10, 5, tzinfo=timezone.utc)]
    tracker = UsageService(env.records, now=lambda: now[0])
    tracker.initialize('insight', insight.id, 'alpha')
    for _ in range(70):
        now[0] += timedelta(seconds=1)
        tracker.record_usage('insight', insight.id, 'alpha', 1)
    row = env.records.read('v2_usage_insight', insight.id).payload
    assert row['count'] == 71
    assert len(row['events']) == 64
    assert row['older_count'] == 7
    assert row['events'][0] == {'at': (now[0] - timedelta(seconds=63)).isoformat(), 'kind': 'use'}
    assert row['events'][-1] == {'at': now[0].isoformat(), 'kind': 'use'}
    assert all(set(event) == {'at', 'kind'} for event in row['events'])


def test_summary_and_note_citations_append_one_document_event(env):
    doc, _ = add_document(env)
    original = env.records.read('documents', doc)
    chosen = [{'layer': 'L2', 'entry': {'document_id': doc}},
              {'layer': 'L1', 'entry': {'document_id': doc}}]
    record_answer_usage(env.records, chosen, [{'n': 1}, {'n': 2}], 'alpha')
    row = env.records.read('v2_usage_document', doc).payload
    assert row['count'] == 2 and len(row['events']) == 1
    assert row['events'][0]['kind'] == 'citation'
    assert env.records.read('documents', doc) == original
