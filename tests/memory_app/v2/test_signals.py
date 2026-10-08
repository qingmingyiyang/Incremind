from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
import asyncio
import sqlite3
import threading
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from core.storage_provider import SQLiteStructuredRecordStore, SQLiteUnitOfWorkConflict
from backend.recognition import RecognitionService, WorkScope


@pytest.fixture
def env(tmp_path):
    from backend.memory_app.v2.signals import SignalService, install_signal_routes
    records = SQLiteStructuredRecordStore(tmp_path / 'signals.sqlite3')
    clock = [datetime(2026, 10, 6, 12, tzinfo=timezone.utc)]
    owner = SignalService(records, now=lambda: clock[0])
    domain = RecognitionService(records)
    source = domain.stage_experience(scope=WorkScope('local-user', 'alpha'), content='synthetic source')
    candidate = domain.propose(scope=WorkScope('local-user', 'alpha'), content='synthetic candidate', source_experience_ids=[source])
    with records.begin() as tx:
        tx.put('v2_projects', 'alpha', {'name':'synthetic', 'scenes':[]}, expected_revision=0)
        tx.put('v2_turns', 'turn-a', {'project_id':'alpha', 'scene':'scene-a', 'intent':'ask',
            'receipt':{'ask':{'answer':'synthetic answer'}}}, expected_revision=0)
        tx.commit()
    app = FastAPI()
    install_signal_routes(app, records=records, service=owner)
    return SimpleNamespace(records=records, owner=owner, clock=clock, candidate=candidate,
        app=app, http=TestClient(app), copy={'project_id':'alpha','kind':'copy','turn_id':'turn-a','client_id':'copy-a'})


def view(env, client='view-a'):
    return {'project_id':'alpha','kind':'view','client_id':client,
        'object':{'kind':'insight','id':env.candidate.id,'revision':env.candidate.revision}}


def trigger(records, operation, collection):
    with sqlite3.connect(records.database_path) as db:
        db.execute(f"CREATE TRIGGER signal_failure BEFORE {operation} ON crp_structured_records WHEN OLD.collection = '{collection}' BEGIN SELECT RAISE(ABORT, 'synthetic fault detail'); END")


def test_http_payload_whitelist_and_client_identity(env):
    assert env.http.post('/api/v2/signals', json=env.copy).status_code == 204
    assert env.http.post('/api/v2/signals', json=env.copy).status_code == 204
    rows = env.records.list('v2_signals')
    assert len(rows) == 1
    assert set(rows[0].payload) == {'kind','project_id','scene','turn_id','object','at','by'}
    assert rows[0].payload == {'kind':'copy','project_id':'alpha','scene':'scene-a', 'turn_id':'turn-a',
        'object':None, 'at':env.clock[0].isoformat(),'by':'user'}
    assert env.http.post('/api/v2/signals', json={**view(env), 'client_id':'copy-a'}).status_code == 409


@pytest.mark.parametrize('changes', [{'kind':'unknown'}, {'kind':'stop'}, {'question':'private synthetic'},
    {'by':'admin'}, {'at':'2020-01-01'}, {'scene':'injected'}, {'client_id':'bad/id'}, {'object':{'kind':'insight','id':'x','revision':True}}])
def test_http_invalid_fields_are_rejected(env, changes):
    assert env.http.post('/api/v2/signals', json={**env.copy, **changes}).status_code == 400
    assert env.records.list('v2_signals') == ()


@pytest.mark.parametrize('body, status', [({'project_id':'other','kind':'copy','turn_id':'turn-a','client_id':'x'},404),
    ({'project_id':'alpha','kind':'copy','turn_id':'missing','client_id':'x'},404),
    ({'project_id':'alpha','kind':'copy','client_id':'x'},400),
    ({'project_id':'alpha','kind':'view','client_id':'x','object':{'kind':'insight','id':'missing','revision':1}},404)])
def test_actual_turn_and_object_qualification(env, body, status):
    assert env.http.post('/api/v2/signals', json=body).status_code == status
    assert env.records.list('v2_signals') == ()


def test_view_revision_and_pending_state_are_required(env):
    assert env.http.post('/api/v2/signals', json={**view(env), 'object':{**view(env)['object'],'revision':999}}).status_code == 409
    assert env.http.post('/api/v2/signals', json={**view(env), 'project_id':'other'}).status_code == 404
    assert env.http.post('/api/v2/signals', json=view(env)).status_code == 204
    env.domain = RecognitionService(env.records)
    env.domain.publish(scope=WorkScope('local-user','alpha'), candidate_id=env.candidate.id, expected_revision=env.candidate.revision, reviewer='local-user')
    assert env.http.post('/api/v2/signals', json=view(env,'view-active')).status_code == 404


def test_copy_rolling_window_and_view_calendar_day(env):
    env.clock[0] = datetime(2026,10,6,23,59,tzinfo=timezone.utc)
    env.http.post('/api/v2/signals', json=env.copy)
    env.http.post('/api/v2/signals', json=view(env))
    env.clock[0] += timedelta(minutes=2)
    env.http.post('/api/v2/signals', json={**env.copy,'client_id':'copy-b'})
    env.http.post('/api/v2/signals', json=view(env,'view-next-day'))
    assert len(env.records.list('v2_signals')) == 3
    env.clock[0] += timedelta(minutes=8)
    env.http.post('/api/v2/signals', json={**env.copy,'client_id':'copy-c'})
    env.http.post('/api/v2/signals', json=view(env,'view-same-day'))
    assert len(env.records.list('v2_signals')) == 4


def test_settings_close_clear_cutoff_cas_and_corrections_preserved(env):
    assert env.http.get('/api/v2/settings/signals').json() == {'enabled':True,'count':0,'retention_days':180,'cleared_at':None,'revision':0}
    env.http.post('/api/v2/signals', json=env.copy)
    before = env.records.read('v2_turns','turn-a')
    with env.records.begin() as tx:
        tx.put('v2_correction_events','confirmed-correction', {'type':'strike','after':'synthetic confirmed'}, expected_revision=0)
        tx.commit()
    assert env.http.patch('/api/v2/settings/signals',json={'enabled':False,'expected_revision':0}).status_code == 200
    assert env.http.patch('/api/v2/settings/signals',json={'enabled':True,'expected_revision':0}).status_code == 409
    env.http.post('/api/v2/signals',json=view(env))
    assert len(env.records.list('v2_signals')) == 1
    env.clock[0] += timedelta(seconds=1)
    result = env.http.post('/api/v2/settings/signals/clear',json={'expected_revision':1})
    assert result.status_code == 200 and result.json() == {'cleared':1}
    current = env.http.get('/api/v2/settings/signals').json()
    assert current == {'enabled':False,'count':0,'retention_days':180,'cleared_at':env.clock[0].isoformat(),'revision':2}
    assert env.records.read('v2_correction_events','confirmed-correction') is not None
    assert env.records.read('v2_turns','turn-a') == before
    assert env.http.post('/api/v2/settings/signals/clear',json={'expected_revision':1}).status_code == 409


def test_clear_rejects_captured_event_and_reopened_setting_does_not_revive(env):
    captured = env.owner.prepare(env.copy)
    env.clock[0] += timedelta(seconds=1)
    env.owner.clear(expected_revision=0)
    assert env.owner.record(captured) is None
    env.owner.set_enabled(False, expected_revision=1)
    while_closed = env.owner.prepare(view(env))
    env.owner.set_enabled(True, expected_revision=2)
    assert env.owner.record(while_closed) is None
    env.clock[0] += timedelta(seconds=1)
    assert env.owner.record(env.owner.prepare(view(env,'fresh'))) is not None


def test_180_day_atomic_rollup_boundary_count_and_idempotency(env):
    env.owner.record(env.owner.prepare(env.copy))
    env.clock[0] += timedelta(days=180)
    env.owner.rollup()
    assert len(env.records.list('v2_signals')) == 1
    env.clock[0] += timedelta(microseconds=1)
    env.owner.rollup()
    assert env.records.list('v2_signals') == ()
    rows = env.records.list('v2_signal_rollups')
    assert len(rows) == 1 and rows[0].payload == {'project_id':'alpha','month':'2026-10','counts':{'copy':1}}
    assert env.owner.settings()['count'] == 1
    env.owner.rollup()
    assert env.records.list('v2_signal_rollups') == rows
    assert env.owner.clear(expected_revision=0) == {'cleared':1}
    assert env.owner.settings()['count'] == 0


def test_rollup_delete_fault_rolls_back_summary_and_raw(env):
    env.owner.record(env.owner.prepare(env.copy))
    env.clock[0] += timedelta(days=181)
    trigger(env.records,'DELETE','v2_signals')
    with pytest.raises(sqlite3.IntegrityError):
        env.owner.rollup()
    assert len(env.records.list('v2_signals')) == 1
    assert env.records.list('v2_signal_rollups') == ()


def test_two_instances_and_maximum_project_multimonth_rollup(env):
    from backend.memory_app.v2.signals import SignalService
    project = 'p' * 128
    with env.records.begin() as tx:
        tx.put('v2_projects',project,{'name':'synthetic','scenes':[]},expected_revision=0)
        tx.put('v2_turns','long-turn',{'project_id':project,'intent':'ask','receipt':{'ask':{'answer':'synthetic'}}},expected_revision=0)
        tx.commit()
    for month in (1,2):
        env.clock[0] = datetime(2026,month,1,tzinfo=timezone.utc)
        env.owner.record(env.owner.prepare({**env.copy,'project_id':project,'turn_id':'long-turn','client_id':f'long-{month}'}))
    env.clock[0] = datetime(2026,10,6,tzinfo=timezone.utc)
    second = SignalService(SQLiteStructuredRecordStore(env.records.database_path),now=lambda:env.clock[0])
    barrier = threading.Barrier(2)
    def run(owner):
        barrier.wait()
        owner.rollup()
    with ThreadPoolExecutor(max_workers=2) as pool:
        list(pool.map(run,[env.owner,second]))
    rows = env.records.list('v2_signal_rollups')
    assert sorted((r.payload['project_id'],r.payload['month'],r.payload['counts']) for r in rows) == [(project,'2026-01',{'copy':1}),(project,'2026-02',{'copy':1})]
    assert env.records.list('v2_signals') == ()


def test_two_instances_same_copy_only_one_write_and_settings_cas(env):
    from backend.memory_app.v2.signals import SignalService
    second = SignalService(SQLiteStructuredRecordStore(env.records.database_path),now=lambda:env.clock[0])
    captures = [(env.owner,env.owner.prepare(env.copy)),(second,second.prepare({**env.copy,'client_id':'copy-other'}))]
    barrier = threading.Barrier(2)
    def write(pair):
        barrier.wait()
        return pair[0].record(pair[1])
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(write,captures))
    assert sum(row is not None for row in results) == 1
    assert len(env.records.list('v2_signals')) == 1
    env.owner.set_enabled(False,expected_revision=0)
    with pytest.raises(SQLiteUnitOfWorkConflict):
        second.clear(expected_revision=0)


def test_response_sent_before_real_sqlite_failure_and_safe_log(env, caplog):
    with sqlite3.connect(env.records.database_path) as db:
        db.execute("CREATE TRIGGER signal_failure BEFORE INSERT ON crp_structured_records WHEN NEW.collection='v2_signals' BEGIN SELECT RAISE(ABORT,'synthetic private fault detail'); END")
    import json
    messages=[]
    async def exercise():
        sent=False
        async def receive():
            nonlocal sent
            if not sent:
                sent=True
                return {'type':'http.request','body':json.dumps(env.copy).encode(),'more_body':False}
            return {'type':'http.disconnect'}
        async def send(message):
            messages.append(message)
            assert env.records.list('v2_signals') == ()
        await env.app({'type':'http','asgi':{'version':'3.0'},'http_version':'1.1','scheme':'http','method':'POST',
            'path':'/api/v2/signals','raw_path':b'/api/v2/signals','query_string':b'', 'headers':[(b'content-type',b'application/json')],
            'client':('127.0.0.1',1234),'server':('test',80)},receive,send)
    asyncio.run(exercise())
    assert messages[0]['status']==204 and messages[-1]['type']=='http.response.body'
    assert 'signal_write_failed exception_type=IntegrityError' in caplog.text
    assert 'private fault detail' not in caplog.text


def test_daily_registration_runs_original_owner(env):
    from backend.memory_app.v2.daily import install_daily_jobs
    env.owner.record(env.owner.prepare(env.copy))
    env.clock[0] += timedelta(days=181)
    jobs = install_daily_jobs(env.app,records=env.records)
    assert jobs.jobs['signals_rollup'].__self__ is env.owner
    jobs.run()
    assert env.records.list('v2_signals') == ()
    assert env.owner.settings()['count'] == 1


from tests.memory_app.v2.test_workbench_ask import env as workbench_env, ask


def test_real_workbench_answer_signal_without_lazy_project_registration(workbench_env):
    answered = ask(workbench_env)
    assert answered.status_code == 200
    turn = answered.json()['turn']['id']
    assert workbench_env.records.read('v2_projects','alpha') is None
    response = workbench_env.http.post('/api/v2/signals',json={'project_id':'alpha','kind':'copy','turn_id':turn,'client_id':'real-answer-copy'})
    assert response.status_code == 204
    assert len(workbench_env.records.list('v2_signals')) == 1
    assert workbench_env.http.get('/api/v2/settings/signals').json()['count'] == 1


@pytest.mark.parametrize('key,value',[('answer','synthetic private'),('by','admin'),('revision',True)])
def test_nested_object_white_list(env,key,value):
    body=view(env)
    body['object'][key]=value
    assert env.http.post('/api/v2/signals',json=body).status_code==400
    assert env.records.list('v2_signals')==()


def test_internal_stop_and_admin_facts_not_user_statistics(env):
    stopped=env.owner.prepare({**env.copy,'kind':'stop','client_id':'stop-admin'},server=True,by='admin')
    assert env.owner.record(stopped).payload['by']=='admin'
    assert env.owner.settings()['count']==0
    env.clock[0]+=timedelta(days=181)
    env.owner.rollup()
    assert env.records.list('v2_signals')==()
    assert env.records.list('v2_signal_rollups')==()
    assert env.owner.settings()['count']==0


def test_clear_and_event_write_interleave_real_transactions(env):
    prepared=env.owner.prepare(env.copy)
    env.clock[0]+=timedelta(seconds=1)
    barrier=threading.Barrier(2)
    def write():
        barrier.wait()
        env.owner.record(prepared)
    def clear():
        barrier.wait()
        env.owner.clear(expected_revision=0)
    with ThreadPoolExecutor(max_workers=2) as pool:
        a,b=pool.submit(write),pool.submit(clear)
        a.result();b.result()
    assert env.owner.settings()['count']==0
    assert env.records.list('v2_signals')==()


def test_disabled_public_endpoint_still_validates_shape_without_object_lookup(env):
    env.owner.set_enabled(False,expected_revision=0)
    assert env.http.post('/api/v2/signals',json={**env.copy,'turn_id':'missing'}).status_code==204
    assert env.http.post('/api/v2/signals',json={**env.copy,'kind':'stop'}).status_code==400
    assert env.records.list('v2_signals')==()


def test_pending_fade_and_merge_not_viewable(env):
    for collection in ('v2_candidate_fade','v2_candidate_merges'):
        with env.records.begin() as tx:
            marker=tx.put(collection,env.candidate.id,{'synthetic':True},expected_revision=0)
            tx.commit()
        assert env.http.post('/api/v2/signals',json=view(env)).status_code==404
        with env.records.begin() as tx:
            tx.delete(collection,marker.object_id,expected_revision=marker.revision)
            tx.commit()
    assert env.records.list('v2_signals')==()


@pytest.mark.parametrize('nested',[False,True])
def test_writer_rechecks_whitelist_after_internal_payload_mutation(env,nested,caplog):
    captured=env.owner.prepare(view(env) if nested else env.copy)
    (captured.payload['object'] if nested else captured.payload)['text']='synthetic private body'
    with pytest.raises(ValueError,match='invalid_signal_payload'):
        env.owner.record(captured)
    env.owner.safe_record(captured)
    assert 'signal_write_failed exception_type=ValueError' in caplog.text
    assert 'synthetic private body' not in caplog.text
    assert env.records.list('v2_signals')==()


@pytest.mark.parametrize('field,value',[('kind','unknown'),('by','someone'),('at','2026-01-01'),('at','invalid'),('project_id','bad/id')])
def test_writer_rechecks_fact_shapes(env,field,value):
    captured=env.owner.prepare(env.copy)
    captured.payload[field]=value
    with pytest.raises(ValueError,match='invalid_signal_payload'):
        env.owner.record(captured)
    assert env.records.list('v2_signals')==()


def test_clear_storage_fault_rolls_back_cutoff_and_original_feedback(env):
    env.owner.record(env.owner.prepare(env.copy))
    before=env.owner.settings()
    trigger(env.records,'DELETE','v2_signals')
    with pytest.raises(sqlite3.IntegrityError):
        env.owner.clear(expected_revision=0)
    assert env.owner.settings()==before
    assert len(env.records.list('v2_signals'))==1


def test_existing_memory_export_reader_and_external_selection_exclude_signals(env):
    from core.memory_core import SQLiteMemoryReader
    from backend.memory_app.v2.external_context import _selections, ExternalContextError
    with env.records.begin() as tx:
        tx.put('memory_atoms','published-atom',{'id':'published-atom','project_id':'alpha','content':'synthetic published'},expected_revision=0)
        tx.commit()
    reader=SQLiteMemoryReader(env.records)
    before=tuple(reader.list('atom'))
    assert len(before)==1
    env.owner.record(env.owner.prepare(env.copy))
    env.clock[0]+=timedelta(days=181)
    env.owner.rollup()
    assert tuple(reader.list('atom'))==before
    assert reader.list('scenario')==() and reader.list('series_memory')==()
    with pytest.raises(ExternalContextError):
        _selections([{'type':'v2_signals','id':'copy-a','revision':1,'project_id':'alpha','layer':'L3','windows':[]}])
