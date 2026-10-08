"""Completion must fence already elapsed time before the next scheduler tick."""
from datetime import datetime, timedelta, timezone
from threading import Event, Thread, current_thread

import pytest

from backend.memory_app.v2.consolidation import Consolidation
from backend.memory_app.v2.daily import DailyJobs
from backend.memory_app.v2.learning_events import COLLECTION
from backend.memory_app.v2.policies import override
from backend.recognition import RecognitionService, WorkScope
from core.document_engine import SQLiteDocumentRepository
from core.storage_provider import SQLiteStructuredRecordStore
from tests.memory_app.v2.test_consolidation_events import CorrectionModel, corrected
from tests.memory_app.v2.test_workbench_ask import assemble, env as _env

env = _env


class ConsistentClock:
    def __init__(self):
        self.seconds = 0.0
        self.origin = datetime.now(timezone.utc).replace(hour=12, minute=0, second=0, microsecond=0)

    def monotonic(self):
        return self.seconds

    def utc(self):
        return self.origin + timedelta(seconds=self.seconds)


def publish_and_correct(service, project, corrections):
    scope = WorkScope('local-user', project)
    experience = service.stage_experience(scope=scope, content='合成计时原件 ' + project)
    candidate = service.propose(scope=scope, content='合成初始判断', source_experience_ids=[experience])
    recognition = service.publish(scope=scope, candidate_id=candidate.id,
                                  expected_revision=candidate.revision, reviewer='local-user')
    for index in range(corrections):
        recognition = service.revise(scope=scope, recognition_id=recognition.id,
                                     expected_revision=recognition.revision, content=f'合成纠正 {project} {index}')


@pytest.mark.parametrize('after_completion', [0.0, 37.0], ids=['immediate-checkpoint', 'later-checkpoint'])
def test_completed_project_counts_only_time_after_its_atomic_completion(tmp_path, after_completion):
    records = SQLiteStructuredRecordStore(tmp_path / 'clock-boundary.sqlite3')
    service, documents = RecognitionService(records), SQLiteDocumentRepository(records)
    publish_and_correct(service, 'alpha', 6)
    publish_and_correct(service, 'beta', 0)
    clock, callbacks = ConsistentClock(), []
    daily = DailyJobs(records=records, clock=clock.monotonic, check_interval=120)
    conso = Consolidation(records, service, documents, now=clock.utc, completion_clock=daily.completion_clock)
    with override(trigger='@2', consolidate='@2'):
        initial = daily.checkpoint()
        assert initial['alpha']['score'] == 7 and initial['beta']['score'] == 1
        assert initial['alpha']['run_seconds'] == initial['beta']['run_seconds'] == 0
        clock.seconds = 37.0
        request = conso.request('alpha', callbacks.append)
        assert len(callbacks) == 1
        clock.seconds = 180.047
        callbacks[0]()
        # The real Consolidation transaction commits its job and reset together.
        assert records.read('v2_consolidation_jobs', request['job_id']).payload['status'] == 'completed'
        assert records.read('v2_consolidation_runs', clock.utc().date().isoformat()).payload['status'] == 'completed'
        reset = records.read(COLLECTION, 'alpha').payload
        assert reset['score'] == reset['run_seconds'] == 0
        assert len(reset['seen_event_ids']) == 7
        clock.seconds += after_completion
        states = daily.checkpoint()
        assert states['alpha']['score'] == 0 and states['beta']['score'] == 1
        assert states['beta']['run_seconds'] == pytest.approx(180.047 + after_completion)
        assert states['alpha']['run_seconds'] == pytest.approx(after_completion)


def test_completion_helper_is_project_specific_and_restart_ignores_old_monotonic_cursor(tmp_path):
    path = tmp_path / 'restart.sqlite3'
    records = SQLiteStructuredRecordStore(path)
    service = RecognitionService(records)
    publish_and_correct(service, 'alpha', 0)
    publish_and_correct(service, 'beta', 0)
    clock = ConsistentClock()
    daily = DailyJobs(records=records, clock=clock.monotonic, check_interval=120)
    fact_collections = ('recognition_experiences', 'recognitions', 'recognition_versions', 'v2_correction_events')
    domain_facts = {name: records.list(name) for name in fact_collections}
    with override(trigger='@2'):
        daily.checkpoint()
        clock.seconds = 180.047
        daily.completed('alpha')
        reset = records.read(COLLECTION, 'alpha').payload
        assert reset['score'] == reset['run_seconds'] == 0
        assert reset['run_clock_reset']['cursor'] == 180.047
        assert len(reset['seen_event_ids']) == 1
        clock.seconds += 37
        state = daily.checkpoint()
        assert state['alpha']['run_seconds'] == pytest.approx(37)
        assert state['beta']['run_seconds'] == pytest.approx(217.047)

        # A real reopened database retains counters; another scheduler clock starts below the old cursor.
        restarted_records = SQLiteStructuredRecordStore(path)
        restarted_clock = ConsistentClock()
        restarted = DailyJobs(records=restarted_records, clock=restarted_clock.monotonic, check_interval=120)
        first = restarted.checkpoint()
        assert first == state
        restarted_clock.seconds = 7
        next_state = restarted.checkpoint()
        assert next_state['alpha']['run_seconds'] == pytest.approx(44)
        assert next_state['beta']['run_seconds'] == pytest.approx(224.047)
        assert next_state['alpha']['seen_event_ids'] == reset['seen_event_ids']
        assert next_state['beta']['score'] == 1
        assert {name: restarted_records.list(name) for name in fact_collections} == domain_facts


def test_reset_clips_only_its_project_and_keeps_sleep_cap_and_backward_clock(tmp_path):
    records = SQLiteStructuredRecordStore(tmp_path / 'sleep.sqlite3')
    service = RecognitionService(records)
    publish_and_correct(service, 'alpha', 0)
    publish_and_correct(service, 'beta', 0)
    clock = ConsistentClock()
    daily = DailyJobs(records=records, clock=clock.monotonic, check_interval=120)
    with override(trigger='@2'):
        daily.checkpoint()
        clock.seconds = 79990
        daily.completed('alpha')
        clock.seconds = 80000
        state = daily.checkpoint()
        assert state['alpha']['run_seconds'] == 10
        assert state['beta']['run_seconds'] == 240
        clock.seconds = 79999
        assert daily.checkpoint() == state
        clock.seconds = 160000
        state = daily.checkpoint()
        assert state['alpha']['run_seconds'] == 250
        assert state['beta']['run_seconds'] == 480


class PausedTickClock(ConsistentClock):
    def __init__(self):
        super().__init__()
        self.sampled, self.resume = Event(), Event()
        self.tick_thread = None

    def monotonic(self):
        sample = super().monotonic()
        if current_thread() is self.tick_thread:
            self.sampled.set()
            assert self.resume.wait(30)
        return sample


def test_completion_between_tick_clock_sample_and_sqlite_transaction_does_not_recredit(tmp_path):
    records = SQLiteStructuredRecordStore(tmp_path / 'interleaved.sqlite3')
    service, documents = RecognitionService(records), SQLiteDocumentRepository(records)
    publish_and_correct(service, 'alpha', 1)
    publish_and_correct(service, 'beta', 0)
    clock, callbacks, results, errors = PausedTickClock(), [], [], []
    daily = DailyJobs(records=records, clock=clock.monotonic, check_interval=120)
    conso = Consolidation(records, service, documents, now=clock.utc, completion_clock=daily.completion_clock)
    # ContextVar overrides are thread-local, as in the real scheduler worker.
    def tick():
        try:
            with override(trigger='@2'):
                results.append(daily.checkpoint())
        except BaseException as error:
            errors.append(error)

    with override(trigger='@2', consolidate='@2'):
        daily.checkpoint()
        conso.request('alpha', callbacks.append)
        clock.seconds = 100
        clock.tick_thread = Thread(target=tick)
        clock.tick_thread.start()
        try:
            assert clock.sampled.wait(5)
            clock.seconds = 180.047
            callbacks[0]()
            assert records.read(COLLECTION, 'alpha').payload['run_seconds'] == 0
        finally:
            clock.resume.set()
            clock.tick_thread.join(30)
        assert not clock.tick_thread.is_alive() and not errors
        assert results[0]['alpha']['run_seconds'] == 0
        assert results[0]['beta']['run_seconds'] == 100
        clock.seconds = 200
        next_state = daily.checkpoint()
        assert next_state['alpha']['run_seconds'] == pytest.approx(19.953)
        assert next_state['beta']['run_seconds'] == 200


def test_installed_manual_route_uses_the_application_daily_clock_owner(tmp_path):
    from fastapi.testclient import TestClient

    records = SQLiteStructuredRecordStore(tmp_path / 'installed.sqlite3')
    service, documents = RecognitionService(records), SQLiteDocumentRepository(records)
    publish_and_correct(service, 'alpha', 1)
    publish_and_correct(service, 'beta', 0)
    app, _ = assemble(tmp_path, records, documents, service, None)
    daily, conso = app.state.memory_daily_jobs, app.state.memory_consolidation
    clock = ConsistentClock()
    daily.clock, conso.now = clock.monotonic, clock.utc
    with override(trigger='@2', consolidate='@2'), TestClient(app) as http:
        assert records.read(COLLECTION, 'alpha').payload['score'] == 2
        clock.seconds = 180.047
        response = http.post('/api/v2/library/consolidate', json={'project_id': 'alpha'})
        assert response.status_code == 200
        job = records.read('v2_consolidation_jobs', response.json()['job_id'])
        assert job.payload['status'] == 'completed'
        reset = records.read(COLLECTION, 'alpha').payload
        assert reset['score'] == reset['run_seconds'] == 0
        assert reset['run_clock_reset'] == daily.completion_clock()
        clock.seconds += 37
        state = daily.checkpoint()
        assert state['alpha']['run_seconds'] == pytest.approx(37)
        assert state['beta']['run_seconds'] == pytest.approx(120)


def test_real_partial_completion_does_not_reset_the_counter_or_its_boundary(env):
    corrected(env)
    clock, callbacks = ConsistentClock(), []
    daily = DailyJobs(records=env.records, clock=clock.monotonic, check_interval=120)
    model = CorrectionModel(event_ids=['unselected-event'])
    conso = Consolidation(env.records, env.service, env.documents, model,
                          now=clock.utc, completion_clock=daily.completion_clock)
    with override(trigger='@2', consolidate='@2'):
        daily.checkpoint()
        clock.seconds = 60
        daily.checkpoint()
        before = env.records.read(COLLECTION, 'alpha')
        clock.seconds = 180.047
        request = conso.request('alpha', callbacks.append)
        outcome = callbacks[0]()
        assert outcome['status'] == 'partial' and outcome['failed_groups'] >= 1
        assert env.records.read('v2_consolidation_jobs', request['job_id']).payload['status'] == 'partial'
        assert env.records.read(COLLECTION, 'alpha') == before
        after = daily.checkpoint()['alpha']
        assert after['score'] == before.payload['score']
        assert after['seen_event_ids'] == before.payload['seen_event_ids']
        assert after['run_seconds'] == pytest.approx(180.047)
