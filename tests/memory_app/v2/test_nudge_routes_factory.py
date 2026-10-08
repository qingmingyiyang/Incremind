"""新 v2 提醒接口只接真实主人；完整工厂只隔离外部 provider 和时钟。"""
import asyncio
from copy import deepcopy
from datetime import datetime

from tests.memory_app.v2.test_nudge_daily_factory import _snapshot
from tests.memory_app.v2.test_reminder_workbench_factory import (
    _assert_no_ordinary_memory_facts,
    _assert_no_route_auxiliary_turns,
    _control_post,
    _control_project,
    _factory_reminder_controls,
)


FACTS = ('v2_reminders', 'v2_turns', 'v2_turn_requests', 'v2_threads', 'v2_projects',
         'workspace_items', 'documents', 'document_revisions', 'document_markdown',
         'recognition_experiences', 'recognition_candidates', 'recognitions')
RAW = '#提醒负控/健康\r\n提醒我明天下午三点喝水\r\n请带蓝色水杯\r\n'


def _reminder(env, key):
    response = _control_post(env, RAW, key=key)
    assert response.status_code == 200, response.text
    turn = response.json()['turn']
    row = env.records.read('v2_reminders', turn['id'])
    assert row is not None and row.revision == 1
    assert set(row.payload) == {'project_id', 'scene', 'at', 'text', 'turn_id', 'state'}
    assert row.payload['project_id'] == env.project['id'] and row.payload['scene'] == '健康'
    assert row.payload['text'] == RAW and row.payload['turn_id'] == turn['id']
    assert row.payload['state'] == 'active'
    assert turn['intent'] == 'remember' and turn['receipt']['remember']['item_id'] is None
    return row


def _quiet(env):
    _assert_no_ordinary_memory_facts(env.records, env.domains.query.documents)
    _assert_no_route_auxiliary_turns(env.records)
    assert env.route_calls == [] and env.wire_calls == []
    assert tuple(env.records.list('workspace_ask_receipts')) == ()
    assert env.app.state.workbench_turn_execution.tasks == {} and not env.app.state.workbench_tasks


def test_full_factory_reminder_get_preserves_raw_six_fields_and_project_scope(tmp_path, monkeypatch):
    with _factory_reminder_controls(tmp_path, monkeypatch) as env:
        row = _reminder(env, 'reminder-http-read')
        other = _control_project(env.http, '提醒乙')
        preserved = _snapshot(env.records, (*FACTS, 'v2_nudges', 'v2_dates', 'v2_nudge_settings'))
        url = '/api/v2/reminders/' + row.object_id
        expected = {'id': row.object_id, **row.payload, 'revision': 1}
        response = env.http.get(url, params={'project_id': env.project['id']})
        assert response.status_code == 200, response.text
        assert response.json() == expected
        assert response.json()['text'] == RAW
        assert env.http.get(url, params={'project_id': other['id']}).status_code == 404
        assert env.http.get('/api/v2/reminders/missing-reminder',
                            params={'project_id': env.project['id']}).status_code == 404
        assert env.http.get(url).status_code == 422
        assert env.http.get(url, params={'project_id': 'bad/project'}).status_code == 400
        assert _snapshot(env.records, preserved) == preserved
        _quiet(env)


def test_full_factory_nudge_list_is_read_only_and_feedback_uses_real_cas(tmp_path, monkeypatch):
    with _factory_reminder_controls(tmp_path, monkeypatch) as env:
        row = _reminder(env, 'nudge-http-feedback')
        other = _control_project(env.http, '提醒乙')
        owner, reminders = env.app.state.memory_nudges, env.app.state.memory_reminders
        due = datetime.fromisoformat(row.payload['at'])
        monkeypatch.setattr(reminders, 'now', lambda: due)
        monkeypatch.setattr(owner, 'now', lambda: due)
        assert reminders.due(env.project['id']) == [{'id': row.object_id, **row.payload, 'revision': 1}]
        preserved = _snapshot(env.records, (*FACTS, 'v2_nudges', 'v2_dates', 'v2_nudge_settings'))
        # 已到期事实不能让 GET 隐式消费、生成或外发。
        response = env.http.get('/api/v2/nudges', params={'project_id': env.project['id']})
        assert response.status_code == 200, response.text
        assert response.json() == []
        assert _snapshot(env.records, preserved) == preserved
        _quiet(env)
        # 数据前提由真实消费者准备；本节点验接口，不冒签调度循环。
        asyncio.run(owner.deliver(env.project['id']))
        rows = env.records.list('v2_nudges')
        assert len(rows) == 1 and rows[0].revision == 2
        delivered = rows[0]
        original_projection = deepcopy(delivered.payload)
        assert original_projection['event_id'] == row.object_id
        assert original_projection['kind'] == 'reminder' and original_projection['state'] == 'delivered'
        assert original_projection['text'] == RAW
        assert original_projection['model_used'] is False and original_projection['egress_receipt_id'] is None
        expected = {'id': delivered.object_id, **original_projection, 'revision': 2}
        preserved = _snapshot(env.records, FACTS)
        projections = _snapshot(env.records, ('v2_nudges', 'v2_dates', 'v2_nudge_settings'))
        assert env.http.get('/api/v2/nudges', params={'project_id': env.project['id']}).json() == [expected]
        assert env.http.get('/api/v2/nudges', params={'project_id': other['id']}).json() == []
        assert env.http.get('/api/v2/nudges').status_code == 422
        assert _snapshot(env.records, projections) == projections
        url = '/api/v2/nudges/' + delivered.object_id
        body = {'project_id': env.project['id'], 'action': 'opened', 'expected_revision': 2}
        assert env.http.patch(url, json={**body, 'project_id': other['id']}).status_code == 404
        assert env.http.patch(url, json={**body, 'expected_revision': 1}).status_code == 409
        assert env.http.patch(url, json={**body, 'action': 'unknown'}).status_code == 400
        assert env.http.patch(url, json={**body, 'expected_revision': True}).status_code == 400
        assert env.http.patch(url, json={**body, 'text': '不能改正文'}).status_code == 400
        assert env.http.patch(url, json={'project_id': env.project['id'], 'action': 'opened'}).status_code == 400
        assert _snapshot(env.records, projections) == projections
        response = env.http.patch(url, json=body)
        assert response.status_code == 200, response.text
        opened = {**expected, 'action': 'opened', 'action_at': due.isoformat(), 'revision': 3}
        assert response.json() == opened
        assert env.http.get('/api/v2/nudges', params={'project_id': env.project['id']}).json() == [opened]
        assert env.http.patch(url, json=body).status_code == 409
        response = env.http.patch(url, json={**body, 'action': 'closed', 'expected_revision': 3})
        assert response.status_code == 200, response.text
        closed = {**opened, 'action': 'closed', 'revision': 4}
        assert response.json() == closed
        current = env.records.read('v2_nudges', delivered.object_id)
        assert current.revision == 4
        assert current.payload == {key: value for key, value in closed.items() if key not in {'id', 'revision'}}
        assert env.http.get('/api/v2/nudges', params={'project_id': env.project['id']}).json() == []
        assert _snapshot(env.records, preserved) == preserved
        _quiet(env)


def test_full_factory_nudge_settings_keep_zero_to_five_and_revision_contract(tmp_path, monkeypatch):
    with _factory_reminder_controls(tmp_path, monkeypatch) as env:
        owner = env.app.state.memory_nudges
        preserved = _snapshot(env.records, (*FACTS, 'v2_nudges', 'v2_dates'))
        url = '/api/v2/settings/nudges'
        response = env.http.get(url)
        assert response.status_code == 200, response.text
        assert response.json() == owner.settings() == {'limit': 2, 'revision': 0}
        assert env.records.read('v2_nudge_settings', 'local') is None
        for limit, revision in ((0, 0), (5, 1)):
            response = env.http.patch(url, json={'limit': limit, 'expected_revision': revision})
            assert response.status_code == 200, response.text
            expected = {'limit': limit, 'revision': revision + 1}
            assert response.json() == owner.settings() == expected
            assert env.http.get(url).json() == expected
        saved = env.records.read('v2_nudge_settings', 'local')
        assert saved.revision == 2 and saved.payload == {'limit': 5}
        for limit in (-1, 6, True, 2.5, '2', None):
            assert env.http.patch(url, json={'limit': limit, 'expected_revision': 2}).status_code == 400
        assert env.http.patch(url, json={'limit': 2, 'expected_revision': 1}).status_code == 409
        assert env.http.patch(url, json={'limit': 2, 'expected_revision': True}).status_code == 400
        assert env.http.patch(url, json={'limit': 2, 'expected_revision': 2, 'enabled': True}).status_code == 400
        assert env.http.patch(url, json={'limit': 2}).status_code == 400
        current = env.records.read('v2_nudge_settings', 'local')
        assert current.revision == saved.revision and current.payload == saved.payload
        assert _snapshot(env.records, preserved) == preserved
        _quiet(env)


def test_full_factory_inactive_nudge_settings_are_unavailable_without_forcing_a_policy(tmp_path, monkeypatch):
    from backend.memory_app.v2.policies import ACTIVE, override

    active_before = dict(ACTIVE)
    assert 'nudge' not in ACTIVE and 'remind' not in ACTIVE
    with _factory_reminder_controls(tmp_path, monkeypatch, selected=False) as env:
        owner = env.app.state.memory_nudges
        assert owner.policy_version is None
        # 原来保存过设置也不能替代当前策略资格；只有此准备阶段显式选测试候选。
        with override(nudge='@2'):
            assert owner.set_limit(0, expected_revision=0) == {'limit': 0, 'revision': 1}
        preserved = _snapshot(env.records, (*FACTS, 'v2_nudges', 'v2_dates', 'v2_nudge_settings'))
        for method, kwargs in (('get', {}), ('patch', {'json': {'limit': 5, 'expected_revision': 1}})):
            response = getattr(env.http, method)('/api/v2/settings/nudges', **kwargs)
            assert response.status_code == 409, response.text
            assert response.json() == {'detail': 'nudge_policy_unavailable'}
        response = env.http.get('/api/v2/nudges', params={'project_id': env.project['id']})
        assert response.status_code == 200 and response.json() == []
        assert _snapshot(env.records, preserved) == preserved
        assert owner.policy_version is None and ACTIVE == active_before
        _quiet(env)
    assert ACTIVE == active_before


def test_full_factory_feedback_rejects_array_and_object_actions_without_any_write(tmp_path, monkeypatch):
    with _factory_reminder_controls(tmp_path, monkeypatch) as env:
        reminder = _reminder(env, 'nudge-invalid-action-http')
        owner, reminders = env.app.state.memory_nudges, env.app.state.memory_reminders
        due = datetime.fromisoformat(reminder.payload['at'])
        monkeypatch.setattr(reminders, 'now', lambda: due)
        monkeypatch.setattr(owner, 'now', lambda: due)
        # 用同一真实消费服务建立已送达投影，不伪造记录或 revision。
        asyncio.run(owner.deliver(env.project['id']))
        rows = env.records.list('v2_nudges')
        assert len(rows) == 1 and rows[0].revision == 2
        row = rows[0]
        assert row.payload['event_id'] == reminder.object_id
        assert row.payload['kind'] == 'reminder' and row.payload['state'] == 'delivered'
        assert row.payload['action'] is None and row.payload['action_at'] is None
        assert row.payload['model_used'] is False and row.payload['egress_receipt_id'] is None
        before_payload = deepcopy(row.payload)
        preserved = _snapshot(env.records, (*FACTS, 'v2_nudges', 'v2_dates', 'v2_nudge_settings'))
        responses = []
        for action in ([], {}):
            responses.append(env.http.patch('/api/v2/nudges/' + row.object_id,
                json={'project_id': env.project['id'], 'action': action, 'expected_revision': 2}))
        # 先核两次实际请求的全事实/CAS前后，再给类型输入合同的首失败。
        assert _snapshot(env.records, preserved) == preserved
        current = env.records.read('v2_nudges', row.object_id)
        assert current.revision == 2 and current.payload == before_payload
        _quiet(env)
        assert [response.status_code for response in responses] == [400, 400], [
            (response.status_code, response.text) for response in responses]
