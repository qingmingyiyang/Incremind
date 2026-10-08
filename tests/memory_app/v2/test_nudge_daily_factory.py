"""完整工厂的每日消费接线，只有外部 provider 和公开时钟被隔离。"""
import asyncio
from copy import deepcopy
from datetime import datetime, time, timedelta, timezone
from functools import wraps
import inspect
from types import SimpleNamespace

from tests.memory_app.v2.test_reminder_workbench_factory import (
    _assert_no_ordinary_memory_facts,
    _assert_no_route_auxiliary_turns,
    _control_post,
    _control_project,
    _factory_reminder_controls,
)


def _snapshot(records, collections):
    return {collection: {row.object_id: (row.revision, deepcopy(row.payload))
                         for row in records.list(collection)}
            for collection in collections}


def test_full_factory_daily_consumer_delivers_cross_project_user_reminders_under_zero_limit(
        tmp_path, monkeypatch):
    from backend.memory_app.v2.daily import DailyJobs
    from backend.memory_app.v2.library import LibraryRead
    from backend.memory_app.v2.nudges import NudgeService
    from backend.memory_app.v2.policies import ACTIVE, version
    from backend.memory_app.v2.reminders import ReminderService
    from backend.memory_app.v2.signals import SignalService
    from backend.memory_app.v2 import workbench
    from backend.memory_app.workspace_intake import WorkspaceIntake

    active_before = dict(ACTIVE)
    with _factory_reminder_controls(tmp_path, monkeypatch) as env:
        intake_calls, extraction_calls = [], []
        original_intake, original_extract = WorkspaceIntake.process, workbench.generate_insights

        @wraps(original_intake)
        async def observed_intake(instance, *args, **kwargs):
            intake_calls.append(1)
            return await original_intake(instance, *args, **kwargs)

        @wraps(original_extract)
        def observed_extract(*args, **kwargs):
            extraction_calls.append(1)
            return original_extract(*args, **kwargs)

        # 只观察真实入口次数，参数、返回、异常和异步协议均原样委托。
        monkeypatch.setattr(WorkspaceIntake, 'process', observed_intake)
        monkeypatch.setattr(workbench, 'generate_insights', observed_extract)
        # 版本只从测试上下文选择，生产默认和全局 ACTIVE 都保持原值。
        assert version('remind') == '@1' and version('nudge') == '@2'
        assert ACTIVE == active_before
        reminders = env.app.state.memory_reminders
        assert isinstance(reminders, ReminderService)
        clock = SimpleNamespace(at=datetime(2026, 10, 8, tzinfo=timezone.utc))
        monkeypatch.setattr(reminders, 'now', lambda: clock.at)
        alpha = _control_project(env.http, 'alpha')
        beta = _control_project(env.http, 'beta')
        assert alpha['id'] != beta['id']
        expected_day = clock.at.astimezone(reminders.local_timezone).date() + timedelta(days=1)
        due_at = datetime.combine(expected_day, time(15), reminders.local_timezone).astimezone(timezone.utc)
        cases = (
            (alpha, '#alpha/健康\r\n提醒我明天下午三点喝水\r\n请带蓝色水杯\r\n', 'daily-alpha-water'),
            (beta, '#beta/健康\r\n提醒我明天下午三点散步\r\n请带运动鞋\r\n', 'daily-beta-walk'),
            (alpha, '#alpha/健康\r\n提醒我明天下午三点休息\r\n关掉台灯\r\n', 'daily-alpha-rest'),
        )
        expected = {}
        for project, raw, key in cases:
            response = _control_post(env, raw, key=key, project=project)
            assert response.status_code == 200, response.text
            result = response.json()
            turn = result['turn']
            assert turn['intent'] == 'remember'
            row = env.records.read('v2_reminders', turn['id'])
            assert row is not None and row.revision == 1
            fact = {'project_id': project['id'], 'scene': '健康', 'at': due_at.isoformat(),
                    'text': raw, 'turn_id': turn['id'], 'state': 'active'}
            assert set(row.payload) == {'project_id', 'scene', 'at', 'text', 'turn_id', 'state'}
            assert row.payload == fact
            expected[row.object_id] = fact
            saved_turn = env.records.read('v2_turns', turn['id'])
            assert saved_turn is not None and saved_turn.payload['project_id'] == project['id']
            assert saved_turn.payload['thread_id'] == result['thread_id'] == turn['thread_id']
            assert saved_turn.payload['intent'] == 'remember' and saved_turn.payload['item_id'] is None
            assert saved_turn.payload['receipt'] == turn['receipt']
            assert turn['receipt']['remember']['item_id'] is None
            assert env.records.read('v2_threads', result['thread_id']).payload['project_id'] == project['id']
            request = env.records.read('v2_turn_requests', key)
            assert request.payload['state'] == 'completed'
            assert request.payload['body'] == {'project_id': project['id'], 'text': raw, 'intent': 'auto'}
            assert request.payload['result'] == result
            replay = _control_post(env, raw, key=key, project=project)
            assert replay.status_code == 200 and replay.json() == result
        assert len(expected) == 3
        assert len(env.records.list('v2_reminders')) == len(env.records.list('v2_turns')) == 3
        assert env.route_calls == [] and env.wire_calls == []
        assert intake_calls == [] and extraction_calls == []
        _assert_no_ordinary_memory_facts(env.records, env.domains.query.documents)
        _assert_no_route_auxiliary_turns(env.records)
        assert tuple(env.records.list('workspace_ask_receipts')) == ()
        assert tuple(env.records.list('v2_workbench_item_states')) == ()
        preserved = _snapshot(env.records, (
            'v2_reminders', 'v2_turns', 'v2_turn_requests', 'v2_threads', 'v2_projects',
            'workspace_items', 'documents', 'document_revisions', 'document_markdown',
            'recognition_experiences', 'recognition_candidates', 'recognitions',
        ))

        # 创建提醒的真实前提通过后才核装配，缺消费者不能靠手动构造掩盖。
        owner = getattr(env.app.state, 'memory_nudges', None)
        assert isinstance(owner, NudgeService), 'full factory has no application-owned NudgeService'
        assert owner.records is reminders.records is env.domains.query.records
        assert owner.records.database_path == env.records.database_path
        assert owner.reminders is reminders
        assert owner.signals is env.app.state.memory_signals
        assert isinstance(owner.signals, SignalService)
        assert owner.signals.records is owner.records
        assert owner.query is env.domains.query
        assert owner.models is env.domains.query.models is env.app.state.product_task_models
        assert isinstance(owner.library, LibraryRead)
        assert owner.library.records is owner.records and owner.library.workspace is env.domains
        assert owner.library.documents is env.domains.query.documents
        assert owner.library.service is env.domains.query.service
        assert owner.policy_version is None
        monkeypatch.setattr(owner, 'now', lambda: clock.at)
        assert owner.local_timezone == reminders.local_timezone
        assert owner.set_limit(0, expected_revision=0) == {'limit': 0, 'revision': 1}
        assert owner.settings() == {'limit': 0, 'revision': 1}
        jobs = env.app.state.memory_daily_jobs
        assert isinstance(jobs, DailyJobs) and jobs.records is owner.records
        assert {'signals_rollup', 'consolidation', 'nudges'} <= jobs.jobs.keys()
        assert callable(jobs.jobs['nudges']) and not inspect.iscoroutinefunction(jobs.jobs['nudges'])
        excluded = tuple(name for name in jobs.jobs if name != 'nudges')
        assert 'nudges' not in excluded and len(excluded) == len(jobs.jobs) - 1
        assert tuple(env.records.list('v2_nudges')) == ()
        assert reminders.due(alpha['id']) == reminders.due(beta['id']) == []
        clock.at = due_at
        # 使用原同步调度器真实回调；不替换 run_once、消费服务或项目枚举。
        asyncio.run(jobs.run_once(exclude=excluded))
        delivered = env.records.list('v2_nudges')
        assert len(delivered) == 3, 'daily consumer lost due reminders or applied the automatic limit'
        assert {row.payload['event_id'] for row in delivered} == set(expected)
        assert {row.payload['project_id'] for row in delivered} == {alpha['id'], beta['id']}
        for row in delivered:
            fact = expected[row.payload['event_id']]
            payload = {**fact, 'state': 'delivered', 'kind': 'reminder',
                       'event_id': fact['turn_id'], 'delivery_at': clock.at.isoformat(),
                       'action': None, 'action_at': None, 'evidence_ids': [],
                       'reminder_revision': 1, 'model_used': False, 'egress_receipt_id': None}
            assert row.revision == 2 and row.payload == payload
        public = owner.list(alpha['id']) + owner.list(beta['id'])
        assert {item['id'] for item in public} == {row.object_id for row in delivered}
        assert {item['id']: item for item in public} == {
            row.object_id: {'id': row.object_id, **row.payload, 'revision': row.revision}
            for row in delivered}
        projection = _snapshot(env.records, ('v2_nudges', 'v2_nudge_settings'))
        assert tuple(env.records.list('v2_dates')) == ()
        assert _snapshot(env.records, preserved) == preserved
        assert env.route_calls == [] and env.wire_calls == []
        assert intake_calls == [] and extraction_calls == []
        _assert_no_ordinary_memory_facts(env.records, env.domains.query.documents)
        _assert_no_route_auxiliary_turns(env.records)
        assert tuple(env.records.list('workspace_ask_receipts')) == ()
        asyncio.run(jobs.run_once(exclude=excluded))
        assert _snapshot(env.records, projection) == projection
        assert _snapshot(env.records, preserved) == preserved
        assert tuple(env.records.list('v2_dates')) == ()
        _assert_no_ordinary_memory_facts(env.records, env.domains.query.documents)
        _assert_no_route_auxiliary_turns(env.records)
        assert tuple(env.records.list('workspace_ask_receipts')) == ()
        assert tuple(env.records.list('v2_workbench_item_states')) == ()
        assert env.route_calls == [] and env.wire_calls == []
        assert intake_calls == [] and extraction_calls == []
        assert env.app.state.workbench_turn_execution.tasks == {}
        assert not env.app.state.workbench_tasks
        assert version('remind') == '@1' and version('nudge') == '@2'
        assert ACTIVE == active_before
    assert ACTIVE == active_before


def test_full_factory_daily_loop_delivers_user_reminder_on_the_next_original_minute_tick(
        tmp_path, monkeypatch):
    import pytest
    from backend.memory_app.v2 import daily
    from backend.memory_app.v2.policies import ACTIVE, version

    active_before = dict(ACTIVE)
    with _factory_reminder_controls(tmp_path, monkeypatch) as env:
        owner, reminders = env.app.state.memory_nudges, env.app.state.memory_reminders
        jobs = env.app.state.memory_daily_jobs
        # 生命周期停止原启动任务后，再从同一主人真实 start/_loop 进入假钟合同。
        env.http.portal.call(jobs.stop)
        assert jobs.task.done()
        assert jobs.initial_delay == jobs.check_interval == 60 and jobs.interval == 86400
        assert version('trigger') == '@2' and version('nudge') == '@2'
        assert owner.reminders is reminders and owner.signals is env.app.state.memory_signals
        clock = SimpleNamespace(at=datetime(2026, 10, 8, tzinfo=timezone.utc), monotonic=0.0)
        monkeypatch.setattr(reminders, 'now', lambda: clock.at)
        monkeypatch.setattr(owner, 'now', lambda: clock.at)
        monkeypatch.setattr(jobs, 'clock', lambda: clock.monotonic)
        raw = '#提醒负控/健康\r\n提醒我明天下午三点喝水\r\n请带蓝色水杯\r\n'
        response = _control_post(env, raw, key='real-minute-loop-reminder')
        assert response.status_code == 200, response.text
        result = response.json()
        turn = result['turn']
        expected_day = clock.at.astimezone(reminders.local_timezone).date() + timedelta(days=1)
        due_at = datetime.combine(expected_day, time(15), reminders.local_timezone).astimezone(timezone.utc)
        original = env.records.read('v2_reminders', turn['id'])
        assert original is not None and original.revision == 1
        assert original.payload == {'project_id': env.project['id'], 'scene': '健康',
            'at': due_at.isoformat(), 'text': raw, 'turn_id': turn['id'], 'state': 'active'}
        assert turn['intent'] == 'remember' and turn['receipt']['remember']['item_id'] is None
        preserved = _snapshot(env.records, ('v2_reminders', 'v2_turns', 'v2_turn_requests',
            'v2_threads', 'v2_projects', 'workspace_items', 'documents', 'document_revisions',
            'document_markdown', 'recognition_experiences', 'recognition_candidates', 'recognitions'))
        clock.at = due_at - timedelta(seconds=90)
        assert reminders.due(env.project['id']) == []
        assert tuple(env.records.list('v2_nudges')) == ()
        sleeps = []

        class MinuteClockAsyncio:
            async def sleep(self, delay):
                # 只替换此模块的时间依赖，原策略仍必须请求完整一分钟。
                assert delay == 60
                sleeps.append(delay)
                if len(sleeps) == 3:
                    raise asyncio.CancelledError
                if len(sleeps) == 2:
                    # 首次真实维护时尚早三十秒，不能提前送达。
                    assert tuple(env.records.list('v2_nudges')) == ()
                clock.monotonic += delay
                clock.at += timedelta(seconds=delay)

            def __getattr__(self, name):
                # create_task、to_thread、CancelledError 等全部委托原 asyncio。
                return getattr(asyncio, name)

        monkeypatch.setattr(daily, 'asyncio', MinuteClockAsyncio())

        async def exercise():
            await jobs.start()
            try:
                with pytest.raises(asyncio.CancelledError):
                    await jobs.task
            finally:
                await jobs.stop()

        asyncio.run(exercise())
        assert jobs.task.done() and jobs.task.cancelled()
        assert sleeps == [60, 60, 60]
        assert clock.monotonic == 120 and clock.at == due_at + timedelta(seconds=30)
        assert jobs.initial_delay == jobs.check_interval == 60 and jobs.interval == 86400
        assert jobs._fallback_elapsed < jobs.interval
        delivered = env.records.list('v2_nudges')
        assert len(delivered) == 1, 'real minute loop did not consume the newly due user reminder'
        row = delivered[0]
        assert row.revision == 2
        assert row.payload == {**original.payload, 'state': 'delivered', 'kind': 'reminder',
            'event_id': original.object_id, 'delivery_at': clock.at.isoformat(),
            'action': None, 'action_at': None, 'evidence_ids': [], 'reminder_revision': 1,
            'model_used': False, 'egress_receipt_id': None}
        assert owner.list(env.project['id']) == [{'id': row.object_id, **row.payload, 'revision': 2}]
        assert _snapshot(env.records, preserved) == preserved
        _assert_no_ordinary_memory_facts(env.records, env.domains.query.documents)
        _assert_no_route_auxiliary_turns(env.records)
        assert tuple(env.records.list('workspace_ask_receipts')) == ()
        assert tuple(env.records.list('v2_dates')) == ()
        assert env.route_calls == [] and env.wire_calls == []
        assert env.app.state.workbench_turn_execution.tasks == {} and not env.app.state.workbench_tasks
        assert version('trigger') == '@2' and version('nudge') == '@2'
        assert ACTIVE == active_before
    assert ACTIVE == active_before
