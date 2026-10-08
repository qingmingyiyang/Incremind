from datetime import datetime, timezone

from fastapi import FastAPI
from fastapi.testclient import TestClient
import pytest

from core.storage_provider import SQLiteStructuredRecordStore


def test_week_uses_beijing_monday_and_half_open_interval(tmp_path):
    from backend.memory_app.v2.stats import install_stats_routes
    records = SQLiteStructuredRecordStore(tmp_path / 'records.sqlite3')
    now = datetime(2026, 10, 4, 16, 0, tzinfo=timezone.utc)  # Monday 00:00 Beijing
    with records.begin() as tx:
        for identity, kind, project, at in (
            ('before', 'remember', 'alpha', '2026-10-04T15:59:59+00:00'),
            ('start', 'remember', 'alpha', '2026-10-04T16:00:00+00:00'),
            ('confirm', 'confirm', 'alpha', '2026-10-05T08:00:00+08:00'),
            ('forget', 'forget', 'beta', '2026-10-06T10:00:00+08:00'),
            ('end', 'remember', 'alpha', '2026-10-11T16:00:00+00:00'),
            ('malformed', 'remember', 'alpha', 'private invalid value'),
        ):
            tx.put('v2_activity', identity, {'kind':kind, 'project_id':project, 'object_id':identity, 'at':at}, expected_revision=0)
        tx.commit()
    app = FastAPI()
    install_stats_routes(app, records=records, now=lambda: now)
    with TestClient(app) as http:
        assert http.get('/api/v2/stats/week').json() == {
            'week_start':'2026-10-05T00:00:00+08:00', 'remember':1, 'confirm':1, 'forget':1, 'forget_auto':0}
        assert http.get('/api/v2/stats/week?project_id=alpha').json() == {
            'week_start':'2026-10-05T00:00:00+08:00', 'remember':1, 'confirm':1, 'forget':0, 'forget_auto':0}
        assert http.get('/api/v2/stats/week?project_id=').status_code == 400


def test_activity_is_idempotent_and_logs_only_exception_type(tmp_path, monkeypatch, caplog):
    from backend.memory_app.v2.stats import record_activity
    records = SQLiteStructuredRecordStore(tmp_path / 'records.sqlite3')
    record_activity(records, 'remember', 'alpha', 'item-1', event_id='remember-item-1')
    record_activity(records, 'remember', 'alpha', 'item-1', event_id='remember-item-1')
    assert len(records.list('v2_activity')) == 1
    assert datetime.fromisoformat(records.list('v2_activity')[0].payload['at']).tzinfo is not None
    def broken(*args, **kwargs):
        raise RuntimeError('PRIVATE CREDENTIAL')
    monkeypatch.setattr(type(records), 'begin', broken)
    assert record_activity(records, 'forget', 'alpha', 'item-1') is None
    assert 'activity_write_failed' in caplog.text and 'RuntimeError' in caplog.text
    assert 'PRIVATE CREDENTIAL' not in caplog.text


from tests.memory_app.v2.test_workbench_ask import env, add_document, publish
from tests.memory_app.v2.test_library_actions import pending, action


def test_remember_reentry_and_confirmation_and_forget_transitions(env):
    import asyncio
    from backend.memory_app.v2.auto_confirm import process_and_confirm
    doc, item = add_document(env)
    asyncio.run(process_and_confirm(env.domains, item, 'alpha'))
    events = env.records.list('v2_activity')
    assert [(row.payload['kind'], row.payload['object_id']) for row in events] == [('remember', item)]
    candidate = pending(env)
    assert action(env, candidate.id, 'confirm', expected_revision=2).status_code == 409
    saved = action(env, candidate.id, 'confirm', expected_revision=1).json()
    assert action(env, saved['id'], 'forget', forgotten=True).status_code == 200
    assert action(env, saved['id'], 'forget', forgotten=True).status_code == 200
    assert action(env, saved['id'], 'forget', forgotten=False).status_code == 200
    kinds = [row.payload['kind'] for row in env.records.list('v2_activity')]
    assert kinds.count('remember') == 1 and kinds.count('confirm') == 1 and kinds.count('forget') == 1
    assert action(env, saved['id'], 'forget', forgotten=True).status_code == 200
    assert len([row for row in env.records.list('v2_activity') if row.payload['kind']=='forget']) == 2


def test_failed_activity_write_preserves_real_confirmation(env, monkeypatch):
    from core.storage_provider.sqlite_uow import SQLiteStructuredRecordUnitOfWork
    original = SQLiteStructuredRecordUnitOfWork.put
    def fail_activity(self, collection, *args, **kwargs):
        if collection == 'v2_activity':
            raise OSError('private storage content')
        return original(self, collection, *args, **kwargs)
    monkeypatch.setattr(SQLiteStructuredRecordUnitOfWork, 'put', fail_activity)
    doc, _ = add_document(env)
    candidate = pending(env)
    response = action(env, candidate.id, 'confirm', expected_revision=1)
    assert doc and response.status_code == 200 and response.json()['state']=='active'
    assert env.records.list('v2_activity') == ()


def test_old_confirmed_item_is_not_backfilled_on_reentry(env):
    import asyncio
    from backend.memory_app.v2.auto_confirm import process_and_confirm
    _, item = add_document(env)
    event = env.records.read('v2_activity', 'remember-' + item)
    with env.records.begin() as tx:
        tx.delete('v2_activity', event.object_id, expected_revision=event.revision)
        tx.commit()
    asyncio.run(process_and_confirm(env.domains, item, 'alpha'))
    assert env.records.list('v2_activity') == ()


@pytest.mark.parametrize("composed", [True, False])
def test_real_document_forget_counts_once_restore_does_not_count(env, monkeypatch, composed):
    if not composed:
        delattr(env.http.app.state, "document_record_activity")
    from types import SimpleNamespace
    from backend.api.routes.product import documents as routes
    doc, _ = add_document(env)
    env.http.app.state.container = SimpleNamespace(root_dir=env.root)
    env.http.app.include_router(routes.router)
    monkeypatch.setattr(routes.product_repositories, '_object_store', lambda root: (None, None))
    monkeypatch.setattr(routes.product_repositories, '_document_repository', lambda *args: env.documents)
    def mutate(verb, revision):
        return env.http.post(f'/api/rebuild/documents/{doc}/{verb}', json={
            'project_id':'alpha', 'expected_revision':revision})
    assert mutate('archive', 2).status_code == 200
    assert mutate('archive', 3).status_code == 409  # Existing repository rejects already-archived documents.
    assert mutate('restore', 3).status_code == 200
    events = [row.payload for row in env.records.list('v2_activity') if row.payload['kind']=='forget']
    assert len(events)==1 and events[0]['object_id']==doc and events[0]['project_id']=='alpha'


def test_activity_failure_does_not_prevent_manual_forget(env, monkeypatch):
    from core.storage_provider.sqlite_uow import SQLiteStructuredRecordUnitOfWork
    saved, _ = publish(env)
    original = SQLiteStructuredRecordUnitOfWork.put
    def failing(self, collection, *args, **kwargs):
        if collection == 'v2_activity':
            raise OSError('private')
        return original(self, collection, *args, **kwargs)
    monkeypatch.setattr(SQLiteStructuredRecordUnitOfWork, 'put', failing)
    assert action(env, saved.id, 'forget', forgotten=True).json()['state']=='forgotten'


def test_inspiration_is_remembered_once_after_commit_and_not_on_replay_or_read(env):
    path = '/api/v2/workbench/turns'
    body = {'intent':'inspiration', 'project_id':'alpha', 'text':'灵感记录'}
    first = env.http.post(path, json=body, headers={'Idempotency-Key':'inspiration-stat'})
    assert first.status_code == 200
    saved = first.json()
    assert env.http.post(path, json=body, headers={'Idempotency-Key':'inspiration-stat'}).json()==saved
    assert env.http.get('/api/v2/workbench/threads/' + saved['thread_id'], params={'project_id':'inbox'}).status_code==200
    events = env.records.list('v2_activity')
    assert len(events)==1 and events[0].payload['kind']=='remember'
    assert events[0].payload['object_id']==saved['turn']['receipt']['inspiration']['insight']['id']
    assert events[0].payload['project_id']=='inbox'
    project = env.http.post('/api/v2/projects', json={'name':'研究'}).json()['id']
    assert env.http.post(path, json={'intent':'inspiration','text':'#研究 一个想法'}).status_code==200
    assert env.http.get('/api/v2/stats/week', params={'project_id':project}).json()['remember']==1


def test_auto_forget_is_separate_and_uses_existing_event_timestamp(tmp_path):
    from backend.memory_app.v2.stats import week_stats
    records = SQLiteStructuredRecordStore(tmp_path / 'records.sqlite3')
    with records.begin() as tx:
        for identity, kind, by, project, field, at in (
            ('old', 'forget', 'auto', 'alpha', 'created_at', '2026-10-04T15:59:59+00:00'),
            ('auto', 'forget', 'auto', 'alpha', 'created_at', '2026-10-04T16:00:00+00:00'),
            ('future', 'forget', 'auto', 'alpha', 'at', '2026-10-11T16:00:00+00:00'),
            ('another', 'forget', 'auto', 'beta', 'at', '2026-10-06T08:00:00+08:00'),
            ('cool', 'cool', 'auto', 'alpha', 'created_at', '2026-10-06T08:00:00+08:00'),
            ('revive', 'revive', 'auto', 'alpha', 'created_at', '2026-10-06T08:00:00+08:00'),
            ('manual', 'forget', 'user', 'alpha', 'at', '2026-10-06T08:00:00+08:00'),
        ):
            tx.put('v2_activity', identity, {'kind':kind, 'by':by, 'project_id':project,
                'object_id':identity, field:at}, expected_revision=0)
        tx.commit()
    now = lambda: datetime(2026,10,6,tzinfo=timezone.utc)
    all_projects = week_stats(records, now=now)
    assert all_projects['forget_auto']==2 and all_projects['forget']==1
    one = week_stats(records, 'alpha', now=now)
    assert one['forget_auto']==1 and one['forget']==1
    assert one['remember']==one['confirm']==0


def test_real_auto_forgetting_is_counted_once_after_commit(env):
    from datetime import timedelta
    from tests.memory_app.v2.test_auto_forget import START, insight, check
    from backend.memory_app.v2.stats import week_stats
    insight(env)
    check(env, 102)
    assert week_stats(env.records, now=lambda: START+timedelta(days=102))['forget_auto']==0
    check(env, 204)
    now = lambda: START+timedelta(days=204)
    first = week_stats(env.records, now=now)
    assert first['forget_auto']==1 and first['forget']==0
    events = env.records.list('v2_activity')
    check(env, 204)
    assert env.records.list('v2_activity')==events
    assert week_stats(env.records, now=now)==first
