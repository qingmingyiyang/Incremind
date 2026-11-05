from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from pathlib import Path
from shutil import copyfile
import sqlite3
import time

from fastapi import HTTPException
from fastapi.testclient import TestClient
import pytest

from core.companion_core import CompanionRepository
from core.companion_core.errors import CompanionConflict
from core.companion_core.reminders import CompanionReminderService
from core.storage_provider import SQLiteStructuredRecordStore
from backend.memory_app.v2.reminders import ReminderService
from backend.memory_app.v2.reminder_center import ReminderCenter, OPERATIONS, BINDINGS


def utc():
    return datetime.now(timezone.utc)


@pytest.fixture
def center(tmp_path):
    records = SQLiteStructuredRecordStore(tmp_path / 'records.sqlite3')
    reminders = ReminderService(records, now=utc, local_timezone=timezone.utc, policy_version='@1')
    legacy = CompanionReminderService(CompanionRepository.at_data_root(tmp_path))
    return ReminderCenter(records, reminders, legacy)


def old(center, *, count=1, due=False):
    return center.legacy.create(title='原手动提醒', scheduled_at=(utc()+timedelta(seconds=1) if due else utc()+timedelta(days=2)).isoformat(),
        timezone_name='UTC', advance_minutes=0, recurrence='daily' if count>1 else 'once', repeat_count=count)


def command(center, item, action='reschedule'):
    row = next(row for row in center.list('default')['items'] if row['id']==item.reminder_id and row['source']=='legacy')
    return {'project_id':'default','action':action,'source':'legacy','id':row['id'],
        'expected_revision':row['revision'],'binding_revision':row['binding_revision'],
        **({'at':(utc()+timedelta(days=5)).isoformat()} if action=='reschedule' else {}),
        **({'occurrence':row['complete_occurrence']} if action=='complete' else {})}


def test_manual_v2_real_date_parse_replay_scope_and_abandon(center):
    body = {'project_id':'default','action':'create','text':'明天喝水'}
    first = center.apply('create-a',body)
    item = first['result']
    assert item['turn_id'] is None and item['source']=='v2'
    assert datetime.fromisoformat(item['scheduled_at']).hour==9
    assert datetime.fromisoformat(item['scheduled_at']).date()==(utc()+timedelta(days=1)).date()
    assert center.apply('create-a',body)==first
    assert len(center.records.list('v2_reminders'))==1
    assert center.list('another')['items']==[]
    with pytest.raises(HTTPException) as conflict:
        center.apply('create-a',{**body,'text':'明天吃饭'})
    assert conflict.value.status_code==409
    pending = center.prepare('create-b',body)
    abandoned = center.abandon('create-b',project_id='default',request=body,expected_revision=pending['revision'])
    assert abandoned['state']=='abandoned'
    assert center.apply('create-b',body)['state']=='abandoned'
    assert len(center.records.list('v2_reminders'))==1
    dropped = center.abandon('never-sent',project_id='default',request=body,expected_revision=0)
    assert dropped['state']=='abandoned' and center.apply('never-sent',body)['state']=='abandoned'


def test_v2_completion_reschedule_and_real_cas(center):
    item = center.apply('new',{'project_id':'default','action':'create','text':'喝水','at':(utc()+timedelta(days=1)).isoformat()})['result']
    action = {'project_id':'default','action':'reschedule','source':'v2','id':item['id'],
              'expected_revision':item['revision'],'binding_revision':0,'at':(utc()+timedelta(days=2)).isoformat()}
    changed = center.apply('change',action)['result']
    assert changed['id']==item['id'] and changed['revision']==2
    with pytest.raises(HTTPException) as conflict:
        center.apply('stale',{key:value for key,value in {**action,'action':'cancel'}.items() if key!='at'})
    assert conflict.value.status_code==409
    done = center.apply('done',{key:value for key,value in {**action,'action':'complete','expected_revision':2}.items() if key!='at'})['result']
    assert done['state']=='done' and done['revision']==3
    assert len(center.records.list('v2_reminders'))==1


def test_legacy_reschedule_retires_original_and_preserves_logical_scope(center):
    item = old(center)
    event = center.legacy.repository.list_reminder_occurrences(item.reminder_id)[0]
    body = command(center,item)
    prepared = center.prepare('move',body)
    locked = center.list('another')['items'][0]
    assert locked['actions']==[] and locked['pending_operation']=={'id':'move','project_id':'default'}
    with pytest.raises(HTTPException):
        center.prepare('second-move',body)
    moved = center.apply('move',body)
    assert moved['state']=='completed' and moved['result']['id']==item.reminder_id
    assert moved['result']['project_id'] is None
    rows = center._legacy_rows()
    assert len(rows)==2 and rows[item.reminder_id].ack_state=='cancelled'
    assert rows[prepared['plan']['target_id']].ack_state=='pending'
    assert len([row for row in center.list('another')['items'] if row['source']=='legacy'])==1
    assert center.apply('move',body)==moved
    with pytest.raises(CompanionConflict):
        center.legacy.present(event_id=event.occurrence_id,expected_revision=event.revision,requires_ack=True)
    with pytest.raises(CompanionConflict):
        center.legacy.act(event_id=event.occurrence_id,expected_revision=event.revision,action='complete')
    assert center._legacy_rows()[item.reminder_id].ack_state=='cancelled'


def test_external_cancel_blocks_automatic_replacement_and_requires_observed_resume(center):
    item = old(center)
    body = command(center,item)
    center.prepare('external',body)
    center.legacy.cancel(reminder_id=item.reminder_id,expected_revision=item.revision)
    blocked = center.apply('external',body)
    assert blocked['state']=='blocked' and len(center._legacy_rows())==1
    assert center.apply('external',body)['state']=='blocked'
    observed = center.read_operation('external','default')
    assert observed['observed']['source_retired'] is True
    finished = center.resume('external',project_id='default',expected_revision=observed['revision'],
                             source_revision=observed['observed']['source_revision'],occurrences=observed['observed']['occurrences'])
    assert finished['state']=='completed' and len(center._legacy_rows())==2


def test_abandon_after_actual_cancel_prevents_any_late_create(center):
    item = old(center)
    body = command(center,item)
    plan = center.prepare('partial',body)
    center._retire_legacy(plan['plan'])
    observed = center.read_operation('partial','default')
    abandoned = center.abandon('partial',project_id='default',request=body,expected_revision=observed['revision'])
    assert abandoned['state']=='abandoned' and abandoned['effects']['source_retired']
    assert center.apply('partial',body)['state']=='abandoned'
    assert len(center._legacy_rows())==1
    assert center.records.read(BINDINGS,item.reminder_id).payload['active_operation'] is None


def test_fixed_target_creation_is_read_back_even_when_user_abandons_waiting(center):
    item = old(center)
    body = command(center,item)
    prepared = center.prepare('actual-target',body)
    plan = prepared['plan']
    center._retire_legacy(plan)
    schedule = plan['target_schedule']
    creator = CompanionReminderService(center.legacy.repository,id_factory=lambda:plan['target_id'])
    creator.create(title=schedule['title'],scheduled_at=schedule['scheduled_at'],timezone_name=schedule['timezone'],
                   advance_minutes=plan['source']['advance_minutes'],recurrence=schedule['recurrence'],repeat_count=schedule['repeat_count'])
    actual = center.abandon('actual-target',project_id='default',request=body,expected_revision=prepared['revision'])
    assert actual['state']=='completed'
    assert center.apply('actual-target',body)==actual
    assert len(center._legacy_rows())==2


def test_real_presented_daily_completion_and_remaining_occurrences(center):
    item = old(center,count=2,due=True)
    time.sleep(1.2)
    dispatch = next(row for row in center.legacy.due_dispatches() if row.event.kind=='reminder_due')
    center.legacy.present(event_id=dispatch.event.event_id,expected_revision=dispatch.occurrence_revision,requires_ack=True)
    completed = center.apply('complete-once',command(center,item,'complete'))
    assert completed['result']['state']=='pending' and completed['result']['remaining_count']==1
    moved = center.apply('remaining',command(center,item))
    target = center._legacy_rows()[center.records.read(BINDINGS,item.reminder_id).payload['current_id']]
    assert moved['state']=='completed' and target.schedule['repeat_count']==1 and target.schedule['recurrence']=='once'


def test_prefetched_real_due_event_race_never_leaves_two_active_plans(center):
    item = old(center,due=True)
    time.sleep(1.2)
    event = next(row for row in center.legacy.due_dispatches() if row.event.kind=='reminder_due')
    body = command(center,item)
    center.prepare('race',body)
    def present():
        try:
            center.legacy.present(event_id=event.event.event_id,expected_revision=event.occurrence_revision,requires_ack=True)
        except CompanionConflict:
            return 'rejected'
        return 'presented'
    with ThreadPoolExecutor(max_workers=2) as pool:
        action = pool.submit(center.apply,'race',body)
        delivery = pool.submit(present)
        result, _ = action.result(), delivery.result()
    if result['state']=='completed':
        assert center._observe_legacy(center.records.read(OPERATIONS,'race').payload['plan'])['source_retired']
        assert len(center._legacy_rows())==2
    else:
        assert result['state']=='blocked' and len(center._legacy_rows())==1


def test_presented_event_is_acknowledged_before_cancel_and_cannot_revive_parent(center):
    item = old(center,due=True)
    time.sleep(1.2)
    dispatch = next(row for row in center.legacy.due_dispatches() if row.event.kind=='reminder_due')
    presented = center.legacy.present(event_id=dispatch.event.event_id,expected_revision=dispatch.occurrence_revision,requires_ack=True)
    moved = center.apply('presented-move',command(center,item))
    assert moved['state']=='completed'
    assert center._legacy_rows()[item.reminder_id].ack_state=='cancelled'
    with pytest.raises(CompanionConflict):
        center.legacy.present(event_id=presented.occurrence_id,expected_revision=presented.revision,requires_ack=True)
    with pytest.raises(CompanionConflict):
        center.legacy.act(event_id=presented.occurrence_id,expected_revision=presented.revision,action='complete')
    center.legacy.act(event_id=presented.occurrence_id,expected_revision=presented.revision,action='acknowledge')
    assert center._legacy_rows()[item.reminder_id].ack_state=='cancelled'


def test_expired_replacement_keeps_actual_partial_effect_and_allows_abandon(center):
    item = old(center)
    body = {**command(center,item),'at':(utc()+timedelta(seconds=.5)).isoformat()}
    center.prepare('expired',body)
    time.sleep(.7)
    blocked = center.apply('expired',body)
    assert blocked['state']=='blocked' and blocked['error']=='reminder_replacement_time_expired'
    assert center.read_operation('expired','default')['observed']['source_retired']
    abandoned = center.abandon('expired',project_id='default',request=body,expected_revision=blocked['revision'])
    assert abandoned['state']=='abandoned' and center.apply('expired',body)['state']=='abandoned'
    assert len(center._legacy_rows())==1


def test_partial_real_ack_resumes_same_frozen_plan_without_dropping_occurrences(center):
    item = old(center,count=2,due=True)
    time.sleep(1.2)
    dispatch = next(row for row in center.legacy.due_dispatches() if row.event.kind=='reminder_due')
    presented = center.legacy.present(event_id=dispatch.event.event_id,expected_revision=dispatch.occurrence_revision,requires_ack=True)
    body = command(center,item)
    prepared = center.prepare('partial-ack',body)
    center.legacy.act(event_id=presented.occurrence_id,action='acknowledge',expected_revision=presented.revision)
    actual = center.read_operation('partial-ack','default')
    assert not actual['observed']['source_retired'] and actual['observed']['planned_count']==2
    with pytest.raises(HTTPException):
        center.resume('partial-ack',project_id='default',expected_revision=actual['revision'],
            source_revision=actual['observed']['source_revision']-1,occurrences=actual['observed']['occurrences'])
    finished = center.resume('partial-ack',project_id='default',expected_revision=actual['revision'],
        source_revision=actual['observed']['source_revision'],occurrences=actual['observed']['occurrences'])
    assert finished['state']=='completed'
    stored = center.records.read(OPERATIONS,'partial-ack').payload['plan']
    assert stored['target_id']==prepared['plan']['target_id']
    assert stored['target_schedule']==prepared['plan']['target_schedule']
    assert finished['result']['repeat_count']==2
    assert center._legacy_rows()[item.reminder_id].ack_state=='cancelled'


def test_real_factory_default_parse_nudge_delivery_and_private_management(tmp_path_factory):
    from backend.memory_app.app import create_app
    from backend.memory_app.v2.privacy import set_private_project
    from backend.memory_app.v2.policies import version
    root = tmp_path_factory.mktemp('factory')
    (root/'config').mkdir()
    copyfile(Path(__file__).parents[3]/'config/settings.toml.example',root/'config/settings.toml')
    app = create_app(runtime_root=root)
    with TestClient(app) as client:
        assert version('trigger')=='@2'
        assert app.state.memory_reminders.policy_version=='@1'
        assert app.state.memory_nudges.manual_reminders is True
        assert app.state.memory_daily_jobs.task is not None and not app.state.memory_daily_jobs.task.done()
        assert app.state.memory_daily_jobs.jobs['nudges'].__self__ is app.state.memory_nudges
        response = client.post('/api/v2/workbench/turns',json={'project_id':'default','text':'提醒我明天早上喝水'})
        assert response.status_code==200,response.text
        assert response.json()['turn']['receipt']['remember']['state']=='done'
        assert app.state.memory_reminders.read(response.json()['turn']['id'],project_id='default')['state']=='active'
        assert app.state.recognition_records.list('v2_route_turn_keys')==()
        center = app.state.memory_reminder_center
        body = {'project_id':'default','action':'create','text':'到点喝水','at':(utc()+timedelta(seconds=2)).isoformat()}
        created = client.post('/api/v2/reminder-center/operations/deliver',json=body)
        assert created.status_code==200 and created.json()['state']=='completed'
        time.sleep(2.2)
        app.state.memory_nudges.run()
        notices = client.get('/api/v2/nudges',params={'project_id':'default'}).json()
        assert any(row['event_id']==created.json()['result']['id'] and row['state']=='delivered' and row['model_used'] is False for row in notices)
        set_private_project(app.state.recognition_records,'default',True,0)
        app.state.memory_nudges.run()
        assert client.get('/api/v2/nudges',params={'project_id':'default'}).json()==[]
        managed = client.get('/api/v2/reminder-center',params={'project_id':'default'}).json()['items']
        assert managed and all(row['private'] for row in managed if row['source']=='v2')
        assert all(row['delivery'] is None for row in managed if row['source']=='v2')
        for path in root.rglob('ai-turns.sqlite3'):
            with sqlite3.connect(path) as database:
                assert database.execute('SELECT count(*) FROM ai_model_attempt_reservations').fetchone()[0]==0
