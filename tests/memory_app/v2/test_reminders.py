"""你定的提醒是独立事实，修订和删除只改变旁路状态。"""

from dataclasses import FrozenInstanceError
from datetime import datetime, timedelta, timezone

import pytest
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient

from core.storage_provider import SQLiteStructuredRecordStore, SQLiteUnitOfWorkConflict
from backend.memory_app.v2.reminders import PreparedReminder, ReminderService
from backend.memory_app.v2.privacy import set_private_project


LOCAL = timezone(timedelta(hours=8))
NOW = datetime(2026, 10, 8, 0, 30, tzinfo=timezone.utc)
TEXT = '提醒我明天下午三点喝水'


@pytest.fixture
def reminders(tmp_path):
    records = SQLiteStructuredRecordStore(tmp_path / 'reminders.sqlite3')
    service = ReminderService(records, now=lambda: NOW, local_timezone=LOCAL,
                              policy_version='@1')
    return records, service


def create(service, *, text=TEXT, project='alpha', turn='turn-1'):
    return service.create(project_id=project, scene='办事', turn_id=turn, text=text)


def test_explicit_reminder_keeps_original_text_scope_turn_and_only_sidecar_fact(reminders):
    records, service = reminders
    text = TEXT + '\r\n'
    result = create(service, text=text)
    assert result['at'] == '2026-10-09T07:00:00+00:00'
    assert result['text'] == text and result['turn_id'] == 'turn-1'
    assert result['project_id'] == 'alpha' and result['scene'] == '办事'
    assert result['state'] == 'active' and result['revision'] == 1
    row = records.read('v2_reminders', result['id'])
    assert row.payload == {key: result[key] for key in
                           ('project_id', 'scene', 'at', 'text', 'turn_id', 'state')}
    assert records.list('documents') == ()
    assert records.list('workspace_items') == ()
    assert records.list('recognitions') == ()
    assert records.list('recognition_candidates') == ()


@pytest.mark.parametrize('text', ['提醒我明天早上之前喝水', '提醒我喝水', '普通资料'])
def test_unparseable_input_returns_to_ordinary_remember_without_saving(reminders, text):
    records, service = reminders
    assert create(service, text=text) is None
    assert records.list('v2_reminders') == ()


def test_same_turn_replay_retains_the_original_time_and_state(reminders):
    records, service = reminders
    result = create(service)
    service.update(result['id'], project_id='alpha', expected_revision=1, state='done')
    later = ReminderService(records, now=lambda: NOW + timedelta(days=2),
                            local_timezone=LOCAL, policy_version='@1')
    replay = create(later)
    assert replay['at'] == result['at'] and replay['state'] == 'done'
    assert replay['revision'] == 2 and len(records.list('v2_reminders')) == 1
    with pytest.raises(HTTPException) as error:
        create(later, text='提醒我明天喝水')
    assert error.value.status_code == 409


def test_stage_joins_the_turn_transaction_and_conflict_rolls_back_both(reminders):
    records, service = reminders
    with pytest.raises(SQLiteUnitOfWorkConflict):
        with records.begin() as tx:
            tx.put('v2_turns', 'turn-1', {'project_id': 'alpha'}, expected_revision=0)
            service.stage(tx, project_id='alpha', scene='办事', turn_id='turn-1',
                          parsed=service.parse(TEXT))
            tx.put('v2_turns', 'turn-1', {'project_id': 'alpha'}, expected_revision=0)
            tx.commit()
    assert records.list('v2_reminders') == () and records.list('v2_turns') == ()
    with records.begin() as tx:
        tx.put('v2_turns', 'turn-1', {'project_id': 'alpha'}, expected_revision=0)
        saved = service.stage(tx, project_id='alpha', scene='办事', turn_id='turn-1',
                              parsed=service.parse(TEXT))
        tx.commit()
    assert records.read('v2_reminders', saved['id']).payload['turn_id'] == 'turn-1'


def test_time_crossing_between_parse_and_save_keeps_the_admitted_instant(tmp_path):
    records = SQLiteStructuredRecordStore(tmp_path / 'crossing.sqlite3')
    clock = [datetime(2026, 10, 8, 0, 59, 59, tzinfo=timezone.utc)]
    service = ReminderService(records, now=lambda: clock[0], local_timezone=LOCAL,
                              policy_version='@1')
    parsed = service.parse('提醒我今天喝水')
    assert isinstance(parsed, PreparedReminder)
    assert parsed.reference == clock[0].isoformat() and parsed.policy_version == '@1'
    with pytest.raises(FrozenInstanceError):
        parsed.at = '2026-10-09T01:00:00+00:00'
    clock[0] += timedelta(seconds=2)
    with records.begin() as tx:
        saved = service.stage(tx, project_id='alpha', scene=None, turn_id='turn-crossing',
                              parsed=parsed)
        tx.commit()
    assert saved['at'] == '2026-10-08T01:00:00+00:00'
    assert service.due('alpha') == [saved]


def test_time_change_is_cas_and_done_delete_keep_original_fact(reminders):
    records, service = reminders
    result = create(service)
    changed = service.update(result['id'], project_id='alpha', expected_revision=1,
                              at='2026-10-10T09:00:00+08:00')
    assert changed['at'] == '2026-10-10T01:00:00+00:00' and changed['revision'] == 2
    with pytest.raises(HTTPException) as error:
        service.update(result['id'], project_id='alpha', expected_revision=1, state='deleted')
    assert error.value.status_code == 409
    deleted = service.update(result['id'], project_id='alpha', expected_revision=2, state='deleted')
    assert deleted['state'] == 'deleted' and deleted['text'] == TEXT
    assert deleted['turn_id'] == 'turn-1' and deleted['revision'] == 3
    assert len(records.list('v2_reminders')) == 1
    assert service.due('alpha', at=NOW + timedelta(days=10)) == []


@pytest.mark.parametrize('at', ['invalid', '2026-10-10T09:00:00', NOW.isoformat(),
                               (NOW - timedelta(seconds=1)).isoformat()])
def test_time_updates_reject_unknown_naive_and_past_times(reminders, at):
    records, service = reminders
    result = create(service)
    with pytest.raises(HTTPException) as error:
        service.update(result['id'], project_id='alpha', expected_revision=1, at=at)
    assert error.value.status_code == 400
    assert records.read('v2_reminders', result['id']).revision == 1


@pytest.mark.parametrize('state', ['bad', None, 1])
def test_state_updates_accept_only_the_three_planned_states(reminders, state):
    records, service = reminders
    result = create(service)
    with pytest.raises(HTTPException) as error:
        service.update(result['id'], project_id='alpha', expected_revision=1, state=state)
    assert error.value.status_code == 400
    assert records.read('v2_reminders', result['id']).revision == 1


def test_due_query_has_a_precise_boundary_and_survives_rebuilding_services(reminders):
    records, service = reminders
    result = create(service)
    at = datetime.fromisoformat(result['at'])
    assert service.due('alpha', at=at - timedelta(microseconds=1)) == []
    assert service.due('alpha', at=at) == [result]
    with records.begin() as tx:
        tx.put('v2_nudges', 'old-projection', {'project_id': 'alpha'}, expected_revision=0)
        tx.commit()
    rebuilt = ReminderService(records, now=lambda: at + timedelta(days=15),
                              local_timezone=LOCAL, policy_version='@1')
    assert rebuilt.due('alpha') == [result]


def test_private_and_other_projects_are_not_delivered_but_fact_is_retained(reminders):
    records, service = reminders
    result = create(service)
    due_at = datetime.fromisoformat(result['at'])
    assert service.due('other', at=due_at) == []
    set_private_project(records, 'alpha', True, expected_revision=0)
    assert service.due('alpha', at=due_at) == []
    assert records.read('v2_reminders', result['id']).payload['text'] == TEXT
    with pytest.raises(HTTPException) as error:
        service.update(result['id'], project_id='other', expected_revision=1, state='done')
    assert error.value.status_code == 404


def test_patch_route_uses_fact_revision_and_keeps_original_text(reminders):
    from backend.memory_app.v2.reminders import install_reminder_routes

    records, service = reminders
    row = create(service)
    app = FastAPI()
    install_reminder_routes(app, service=service)
    with TestClient(app) as client:
        route = '/api/v2/reminders/' + row['id']
        changed = client.patch(route, json={'at': '2026-10-10T09:00:00+08:00',
                                             'expected_revision': 1})
        assert changed.status_code == 200
        assert changed.json()['at'] == '2026-10-10T01:00:00+00:00'
        conflict = client.patch(route, json={'state': 'deleted', 'expected_revision': 1})
        assert conflict.status_code == 409
        deleted = client.patch(route, json={'state': 'deleted', 'expected_revision': 2})
        assert deleted.status_code == 200 and deleted.json()['text'] == TEXT
    assert records.read('v2_reminders', row['id']).payload['state'] == 'deleted'


@pytest.mark.parametrize('body', [
    {'text': '改原话', 'expected_revision': 1},
    {'state': 'deleted'},
    {'state': 'deleted', 'expected_revision': True},
    {'state': 'deleted', 'expected_revision': 0},
    {'expected_revision': 1},
])
def test_patch_rejects_unknown_fields_and_invalid_cas_before_writing(reminders, body):
    from backend.memory_app.v2.reminders import install_reminder_routes

    records, service = reminders
    row = create(service)
    app = FastAPI()
    install_reminder_routes(app, service=service)
    with TestClient(app) as client:
        assert client.patch('/api/v2/reminders/' + row['id'], json=body).status_code == 400
    assert records.read('v2_reminders', row['id']).revision == 1
