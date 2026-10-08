"""Real domain corrections and restart-safe elapsed running time."""
import asyncio
import pytest

from backend.memory_app.v2.daily import DailyJobs
from backend.memory_app.v2.policies import get, override
from backend.recognition import RecognitionService, WorkScope
from core.storage_provider import SQLiteStructuredRecordStore
from tests.memory_app.v2.test_workbench_ask import env as _env

env = _env


def facts(tmp_path, edits=10):
    records = SQLiteStructuredRecordStore(tmp_path / 'facts.sqlite3')
    service = RecognitionService(records)
    scope = WorkScope('local-user', 'alpha')
    experience = service.stage_experience(scope=scope, content='Synthetic source')
    candidate = service.propose(scope=scope, content='Initial choice', source_experience_ids=[experience])
    recognition = service.publish(scope=scope, candidate_id=candidate.id, expected_revision=1, reviewer='local-user')
    for index in range(edits):
        recognition = service.revise(scope=scope, recognition_id=recognition.id,
            expected_revision=recognition.revision, content=f'Choice corrected {index}')
    return records


def test_real_corrections_count_once_and_stop_does_not_count(tmp_path):
    records = facts(tmp_path)
    clock = [0.0]
    with override(trigger='@2'):
        jobs = DailyJobs(records=records, clock=lambda: clock[0], check_interval=60)
        state = jobs.checkpoint()['alpha']
        assert state['score'] == 11 and state['run_seconds'] == 0
        clock[0] = 60
        assert jobs.checkpoint()['alpha']['run_seconds'] == 60
        restarted = DailyJobs(records=records, clock=lambda: 50000.0, check_interval=60)
        state = restarted.checkpoint()['alpha']
        assert state['score'] == 11 and state['run_seconds'] == 60


def test_sleep_jump_caps_at_two_checks_and_future_clock_is_not_counted(tmp_path):
    records = facts(tmp_path, edits=0)
    clock = [0.0]
    with override(trigger='@2'):
        jobs = DailyJobs(records=records, clock=lambda: clock[0], check_interval=60)
        jobs.checkpoint()
        clock[0] = 80000
        assert jobs.checkpoint()['alpha']['run_seconds'] == 120
        clock[0] = 79999
        assert jobs.checkpoint()['alpha']['run_seconds'] == 120


def test_threshold_requires_ten_events_and_two_hours_running():
    policy = get('trigger', version='@2')
    assert policy(None, score=9, run_seconds=7200, operation='due') is False
    assert policy(None, score=10, run_seconds=7199, operation='due') is False
    assert policy(None, score=10, run_seconds=7200, operation='due') is True
    assert policy(None, elapsed=86400, check_interval=60, operation='elapsed') == 120
    assert policy(None, elapsed=-1, check_interval=60, operation='elapsed') == 0


def test_checkpoint_resets_only_after_successful_project_completion(tmp_path):
    records = facts(tmp_path)
    clock = [0.0]
    with override(trigger='@2'):
        jobs = DailyJobs(records=records, clock=lambda: clock[0], check_interval=60)
        jobs.checkpoint()
        clock[0] = 60
        jobs.checkpoint()
        jobs.completed('alpha')
        state = jobs.checkpoint()['alpha']
        assert state['score'] == 0 and state['run_seconds'] == 0
        assert len(state['seen_event_ids']) == 11


def test_real_organized_material_and_confirmation_count_once_but_questions_do_not(env):
    from tests.memory_app.v2.test_workbench_ask import add_document, publish, ask
    from backend.memory_app.v2.learning_events import events
    document = add_document(env, original='合成计分原件')[0]
    recognition, _ = publish(env, doc=document)
    initial = events(env.records)
    assert len(initial['alpha']) == 2
    assert any(identity.startswith('organized:') for identity in initial['alpha'])
    assert any(identity.startswith('confirm:') for identity in initial['alpha'])
    ask(env, text='这份合成计分原件怎么用？')
    assert events(env.records) == initial


def test_new_scheduler_keeps_startup_maintenance_without_startup_consolidation(tmp_path, monkeypatch):
    records = facts(tmp_path, edits=0)
    clock, calls, sleeps = [0.0], [], []
    jobs = DailyJobs(records=records, clock=lambda: clock[0], initial_delay=60, check_interval=60)
    jobs.register('consolidation', lambda *args: calls.append(('consolidation', args)))
    jobs.register('maintenance', lambda: calls.append(('maintenance', ())))
    async def sleep(delay):
        sleeps.append(delay)
        if len(sleeps) == 2:
            raise asyncio.CancelledError
        clock[0] += delay
    monkeypatch.setattr(asyncio, 'sleep', sleep)
    with override(trigger='@2'), pytest.raises(asyncio.CancelledError):
        asyncio.run(jobs._loop())
    assert calls == [('maintenance', ())] and sleeps == [60, 60]


def test_twenty_four_hour_fallback_survives_sleep_without_crediting_runtime(tmp_path, monkeypatch):
    records = facts(tmp_path, edits=0)
    clock, calls, sleeps = [0.0], [], []
    jobs = DailyJobs(records=records, clock=lambda: clock[0], initial_delay=60, check_interval=60)
    jobs.register('consolidation', lambda *args: calls.append(args))
    async def sleep(delay):
        sleeps.append(delay)
        if len(sleeps) == 3:
            raise asyncio.CancelledError
        clock[0] += delay if len(sleeps) == 1 else 86400
    monkeypatch.setattr(asyncio, 'sleep', sleep)
    with override(trigger='@2'), pytest.raises(asyncio.CancelledError):
        asyncio.run(jobs._loop())
    assert calls == [()]
    assert records.read('v2_learning_accumulation', 'alpha').payload['run_seconds'] == 120


def test_ten_corrections_trigger_only_their_project_after_two_hours_of_real_ticks(tmp_path, monkeypatch):
    records = facts(tmp_path)
    clock, calls = [0.0], []
    jobs = DailyJobs(records=records, clock=lambda: clock[0], initial_delay=60, check_interval=60)
    def consolidate(project):
        calls.append((project, clock[0]))
        jobs.completed(project)
    jobs.register('consolidation', consolidate)
    async def sleep(delay):
        if calls:
            raise asyncio.CancelledError
        clock[0] += delay
    monkeypatch.setattr(asyncio, 'sleep', sleep)
    with override(trigger='@2'), pytest.raises(asyncio.CancelledError):
        asyncio.run(jobs._loop())
    assert calls == [('alpha', 7200)]
    assert records.read('v2_learning_accumulation', 'alpha').payload['score'] == 0


def test_early_job_failure_keeps_scheduler_and_fallback_alive(tmp_path, monkeypatch):
    records = facts(tmp_path)
    clock, calls, ticks = [0.0], [], [0]
    jobs = DailyJobs(records=records, clock=lambda: clock[0], initial_delay=60, check_interval=60)
    def consolidate(*args):
        calls.append(args)
        if args:
            raise RuntimeError('synthetic source drift')
    jobs.register('consolidation', consolidate)
    async def sleep(delay):
        if ticks[0] == 122:
            raise asyncio.CancelledError
        ticks[0] += 1
        clock[0] += delay if ticks[0] < 122 else 86400
    monkeypatch.setattr(asyncio, 'sleep', sleep)
    with override(trigger='@2'), pytest.raises(asyncio.CancelledError):
        asyncio.run(jobs._loop())
    assert calls[:2] == [('alpha',), ('alpha',)]
    assert calls[-1] == ()
