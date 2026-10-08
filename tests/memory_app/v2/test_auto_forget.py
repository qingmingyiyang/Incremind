from datetime import datetime, timedelta, timezone
import asyncio

import pytest

from backend.memory_app.v2.usage import UsageService, decayed_score, half_life_days, recall_weight
from backend.memory_app.v2.auto_forget import AutoForget
from backend.memory_app.recall_preferences import COLLECTION, set_preference
from backend.recognition import WorkScope
from tests.memory_app.v2.test_workbench_ask import env, publish, add_document


START = datetime(2025, 1, 1, tzinfo=timezone.utc)


def age(env, collection, identity, date=START):
    row = env.records.read(collection, identity)
    with env.records.begin() as tx:
        tx.put(collection, identity, {**row.payload, 'created_at': date.isoformat()}, expected_revision=row.revision)
        tx.commit()


def tracker(env, days=0):
    return UsageService(env.records, now=lambda: START + timedelta(days=days))


def check(env, days):
    return AutoForget(env.records, now=lambda: START + timedelta(days=days)).run()


def state(env, recognition):
    row = env.records.read(COLLECTION, recognition.id)
    return row.payload['state'] if row else 'normal'


def insight(env, project='alpha'):
    recognition, _ = publish(env, project=project)
    age(env, 'recognitions', recognition.id)
    tracker(env).initialize('insight', recognition.id, project)
    return recognition


def test_half_life_strengthens_with_count_and_self_reference():
    assert half_life_days(1, 'alpha') == pytest.approx(50.7944154)
    assert half_life_days(5, 'alpha') == pytest.approx(83.7527841)
    assert half_life_days(20, 'alpha') == pytest.approx(121.3356731)
    assert half_life_days(5, 'me') == pytest.approx(3 * half_life_days(5, 'alpha'))
    for count in (1, 5, 20):
        payload = {'project_id': 'alpha', 'score': 2, 'count': count, 'updated_at': START.isoformat()}
        assert decayed_score(payload, START + timedelta(days=half_life_days(count, 'alpha'))) == pytest.approx(1)


def test_protection_and_strict_thresholds(env):
    recognition = insight(env)
    doc, _ = add_document(env)
    age(env, 'documents', doc)
    tracker(env).initialize('document', doc, 'alpha')
    with env.records.begin() as tx:
        row = tx.read('v2_usage_insight', recognition.id)
        tx.put(row.collection, row.object_id, {**row.payload, 'score': 0}, expected_revision=row.revision)
        tx.commit()
    check(env, 29)
    assert state(env, recognition) == 'normal'
    assert env.records.read('v2_document_recall', doc) is None
    check(env, 30)
    assert state(env, recognition) == 'forgotten'
    assert env.records.read(COLLECTION, recognition.id).payload['by'] == 'auto'


def test_cooling_forgetting_persona_and_idempotence_preserve_recognitions(env):
    ordinary, persona = insight(env), insight(env, 'me')
    originals = [env.records.read('recognitions', r.id) for r in (ordinary, persona)]
    check(env, 101)
    assert state(env, ordinary) == 'normal'
    check(env, 102)
    assert state(env, ordinary) == 'cooled'
    assert recall_weight(env.records, 'insight', ordinary.id) == .5
    assert tracker(env).recall_weight('insight', ordinary.id) == .5
    check(env, 203)
    assert state(env, ordinary) == 'cooled'
    check(env, 204)
    assert state(env, ordinary) == 'forgotten'
    assert recall_weight(env.records, 'insight', ordinary.id) == 0
    check(env, 620)
    assert state(env, persona) == 'cooled'
    rows = env.records.list('v2_activity')
    check(env, 620)
    assert env.records.list('v2_activity') == rows
    assert all(row.payload['by'] == 'auto' for row in rows)
    assert [env.records.read('recognitions', r.id) for r in (ordinary, persona)] == originals


def test_use_revives_cooling_but_never_revives_automatic_or_manual_forgetting(env):
    cooled, forgotten, manual = insight(env), insight(env), insight(env)
    scope = WorkScope('local-user', 'alpha')
    set_preference(env.records, scope, manual.id, recognition_revision=2, preference_revision=0, state='forgotten')
    check(env, 102)
    tracker(env, 102).record_usage('insight', cooled.id, 'alpha', 1)
    check(env, 102)
    assert state(env, cooled) == 'normal'
    assert env.records.read('v2_usage_insight', cooled.id).payload['count'] == 2
    check(env, 204)
    assert state(env, forgotten) == 'forgotten'
    tracker(env, 204).record_usage('insight', forgotten.id, 'alpha', 5)
    tracker(env, 204).record_usage('insight', manual.id, 'alpha', 5)
    check(env, 204)
    assert state(env, forgotten) == state(env, manual) == 'forgotten'
    pref = env.records.read(COLLECTION, manual.id)
    assert pref.payload['by'] == 'user'
    set_preference(env.records, scope, manual.id, recognition_revision=2, preference_revision=pref.revision, state='normal')
    assert env.records.read('v2_usage_insight', manual.id).payload['score'] == 1
    assert env.records.read('v2_usage_insight', manual.id).payload['count'] == 3


def test_document_and_summary_scores_use_the_same_weight_without_body_changes(env):
    doc, _ = add_document(env, summary='alpha', body='alpha original')
    age(env, 'documents', doc)
    tracker(env).initialize('document', doc, 'alpha')
    before = env.records.read('documents', doc)
    candidates = env.domains.query.collect_candidates('alpha', 'alpha')['candidates']
    baseline = {row['layer']: row['score'] for row in candidates if row['entry']['id'] == doc}
    check(env, 102)
    assert recall_weight(env.records, 'summary', doc) == .5
    assert env.records.read('v2_document_recall', doc).payload['state'] == 'cooled'
    after = env.domains.query.collect_candidates('alpha', 'alpha')['candidates']
    scores = {row['layer']: row['score'] for row in after if row['entry']['id'] == doc}
    assert scores.keys() == baseline.keys() and {'L1', 'L2'} <= scores.keys()
    assert all(scores[layer] == baseline[layer] * .5 for layer in baseline)
    check(env, 1000)
    assert env.records.read('v2_document_recall', doc).payload['state'] == 'cooled'
    assert env.records.read('documents', doc) == before
    tracker(env, 1000).record_usage('document', doc, 'alpha', 1)
    check(env, 1000)
    assert recall_weight(env.records, 'document', doc) == 1


def test_cas_conflict_skips_one_object_without_partial_activity(env, monkeypatch):
    from core.storage_provider import SQLiteStructuredRecordUnitOfWork, SQLiteUnitOfWorkConflict
    first, second = insight(env), insight(env)
    original = SQLiteStructuredRecordUnitOfWork.put
    def conflict(self, collection, identity, *args, **kwargs):
        if collection == COLLECTION and identity == first.id:
            raise SQLiteUnitOfWorkConflict('synthetic conflict')
        return original(self, collection, identity, *args, **kwargs)
    monkeypatch.setattr(SQLiteStructuredRecordUnitOfWork, 'put', conflict)
    check(env, 102)
    assert state(env, first) == 'normal' and state(env, second) == 'cooled'
    assert len(env.records.list('v2_activity')) == 1


def test_legacy_manual_preference_stays_manual(env):
    recognition = insight(env)
    with env.records.begin() as tx:
        tx.put(COLLECTION, recognition.id, {'id':recognition.id, 'user_id':'local-user', 'project_id':'alpha', 'state':'forgotten'}, expected_revision=0)
        tx.commit()
    before = env.records.read(COLLECTION, recognition.id)
    check(env, 1000)
    assert env.records.read(COLLECTION, recognition.id) == before


def test_daily_registry_runs_at_start_and_every_24_hours_with_clean_shutdown(monkeypatch, caplog):
    from backend.memory_app.v2.daily import DailyJobs
    calls, delays = [], []
    registry = DailyJobs()
    registry.register('first', lambda: calls.append('first'))
    def broken():
        raise OSError('private synthetic details')
    registry.register('broken', broken)
    registry.register('last', lambda: calls.append('last'))
    with pytest.raises(ValueError):
        registry.register('first', lambda: None)
    async def exercise():
        ticked, hold = asyncio.Event(), asyncio.Event()
        async def sleep(seconds):
            delays.append(seconds)
            if len(delays) > 2:
                ticked.set()
                await hold.wait()
        monkeypatch.setattr(asyncio, 'sleep', sleep)
        await registry.start()
        await asyncio.wait_for(ticked.wait(), timeout=2)
        await registry.stop()
        assert registry.task.done()
    asyncio.run(exercise())
    assert calls == ['first', 'last', 'first', 'last']
    assert delays == [60, 86400, 86400]
    assert 'private synthetic details' not in caplog.text


def test_library_exposes_automatic_and_manual_origins_without_changing_content(env):
    automatic, manual = insight(env), insight(env)
    scope = WorkScope('local-user', 'alpha')
    set_preference(env.records, scope, manual.id, recognition_revision=2, preference_revision=0, state='forgotten')
    originals = [env.records.read('recognitions', r.id) for r in (automatic, manual)]
    check(env, 204)
    response = env.http.get('/api/v2/library/insights?project_id=alpha')
    assert response.status_code == 200
    rows = {row['id']:row for row in response.json()['items']}
    assert rows[automatic.id]['state'] == rows[manual.id]['state'] == 'forgotten'
    assert rows[automatic.id]['recall_by'] == 'auto'
    assert rows[manual.id]['recall_by'] == 'user'
    assert [env.records.read('recognitions', r.id) for r in (automatic, manual)] == originals
