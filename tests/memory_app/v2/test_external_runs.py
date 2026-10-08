"""真实 SQLite 运行占位、终态 CAS 与每用户并发控制。"""
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from pathlib import Path
import threading

import pytest

from core.storage_provider import SQLiteStructuredRecordStore
from backend.memory_app.v2.external_runs import ExternalRuns, ExternalRunError, ExternalRunConflict, ExternalRunBusy


@pytest.fixture
def env(tmp_path):
    records = SQLiteStructuredRecordStore(tmp_path / 'runs.sqlite3')
    with records.begin() as tx:
        tx.commit()
    clock = [datetime(2026, 10, 6, 8, tzinfo=timezone.utc)]
    owner = ExternalRuns(records, now=lambda: clock[0].isoformat())
    return records, owner, clock, tmp_path


def reserve(owner, folder, turn='turn-1', user='user-a', **changes):
    values = dict(owner_id=user, executor='codex', adapter_version='codex@1',
        cli_version='0.156.1', preset='workspace', workspace=folder)
    values.update(changes)
    return owner.reserve(turn, **values)


def running(env, turn='turn-1'):
    _, owner, clock, folder = env
    saved = reserve(owner, folder, turn)
    clock[0] += timedelta(seconds=1)
    started = owner.mark_started(turn, owner_id='user-a', expected_revision=saved.run['revision'])
    return started.run


def test_reserve_records_only_intent_and_duplicate_never_authorizes_another_launch(env):
    records, owner, clock, folder = env
    first = reserve(owner, folder)
    assert first.reservation_created is True and first.run['revision'] == 1
    assert first.run['status'] == 'reserved'
    assert first.run['started_at'] is None and first.run['ended_at'] is None
    assert first.run['reserved_at'] == clock[0].isoformat()
    assert first.run['usage'] is None and first.run['exit_code'] is None
    assert first.run['workspace'] == str(folder)
    second = reserve(owner, folder)
    assert second.reservation_created is False and second.run == first.run
    assert len(records.list('v2_external_runs')) == 1
    assert records.read('v2_external_run_slots', 'user-a').payload == {
        'owner_id': 'user-a', 'claims': [{'turn_id': 'turn-1', 'revision': 1}]}
    first.run['status'] = 'changed-in-memory'
    assert owner.read('turn-1', owner_id='user-a')['status'] == 'reserved'


@pytest.mark.parametrize('same_turn,limit', [(False, 1), (False, 3), (True, 1)])
def test_two_store_instances_and_barrier_reserve_have_atomic_limits(env, same_turn, limit):
    records, _, clock, folder = env
    owners = [ExternalRuns(SQLiteStructuredRecordStore(records.database_path), limit=limit,
        now=lambda: clock[0].isoformat()) for _ in range(2)]
    barrier = threading.Barrier(8)
    def attempt(index):
        barrier.wait()
        try:
            return reserve(owners[index % 2], folder, 'same-turn' if same_turn else f'turn-{index}').reservation_created
        except ExternalRunBusy:
            return 'busy'
    with ThreadPoolExecutor(max_workers=8) as pool:
        results = list(pool.map(attempt, range(8)))
    expected = 1 if same_turn else limit
    assert results.count(True) == expected
    assert results.count(False if same_turn else 'busy') == 8 - expected
    assert len(records.list('v2_external_runs')) == expected
    slot = records.read('v2_external_run_slots', 'user-a')
    assert len(slot.payload['claims']) == expected
    assert all(claim['revision'] == 1 for claim in slot.payload['claims'])


def test_each_owner_has_independent_limit_and_other_owner_cannot_read_or_finish(env):
    records, owner, _, folder = env
    first = reserve(owner, folder)
    second = reserve(owner, folder, 'other-turn', 'user-b')
    assert first.reservation_created and second.reservation_created
    with pytest.raises(ExternalRunBusy, match='^external_run_busy$'):
        reserve(owner, folder, 'third-turn')
    with pytest.raises(ExternalRunError, match='^external_run_not_found$'):
        owner.read('turn-1', owner_id='user-b')
    with pytest.raises(ExternalRunConflict, match='^external_run_conflicted$'):
        owner.finish('turn-1', owner_id='user-b', expected_revision=1, status='cancelled', exit_code=None)
    assert records.read('v2_external_runs', 'turn-1').revision == 1


def test_mark_started_has_real_timestamp_unique_cas_and_updates_slot_binding(env):
    records, owner, clock, folder = env
    saved = reserve(owner, folder)
    clock[0] += timedelta(seconds=1)
    started = owner.mark_started('turn-1', owner_id='user-a', expected_revision=1)
    assert started.changed is True and started.run['revision'] == 2
    assert started.run['status'] == 'running' and started.run['started_at'] == clock[0].isoformat()
    assert records.read('v2_external_run_slots', 'user-a').payload['claims'] == [{'turn_id': 'turn-1', 'revision': 2}]
    again = owner.mark_started('turn-1', owner_id='user-a', expected_revision=2)
    assert again.changed is False and again.run == started.run
    with pytest.raises(ExternalRunConflict, match='^external_run_conflicted$'):
        owner.mark_started('turn-1', owner_id='user-a', expected_revision=saved.run['revision'])


@pytest.mark.parametrize('status,code', [('completed', 0), ('failed', 7), ('cancelled', None),
    ('timed_out', None), ('output_limit', None)])
def test_each_actual_terminal_releases_slot_and_retains_unknown_usage_as_null(env, status, code):
    records, owner, clock, folder = env
    started = running(env)
    clock[0] += timedelta(seconds=1)
    final = owner.finish('turn-1', owner_id='user-a', expected_revision=started['revision'], status=status, exit_code=code)
    assert final.changed and final.run['revision'] == 3
    assert final.run['ended_at'] == clock[0].isoformat() and final.run['usage'] is None
    assert final.run['exit_code'] == code and final.run['status'] == status
    assert records.read('v2_external_run_slots', 'user-a').payload['claims'] == []
    assert reserve(owner, folder, 'next-turn').reservation_created
    with pytest.raises(ExternalRunConflict, match='^external_run_conflicted$'):
        owner.mark_started('turn-1', owner_id='user-a', expected_revision=3)


def test_reserved_cancel_does_not_invent_start_and_crash_cannot_expire_occupancy(env):
    records, owner, clock, folder = env
    reserve(owner, folder)
    clock[0] += timedelta(days=365)
    restarted = ExternalRuns(SQLiteStructuredRecordStore(records.database_path), now=lambda: clock[0].isoformat())
    with pytest.raises(ExternalRunBusy):
        reserve(restarted, folder, 'next-turn')
    final = restarted.finish('turn-1', owner_id='user-a', expected_revision=1, status='cancelled', exit_code=None)
    assert final.run['started_at'] is None and final.run['revision'] == 2
    assert reserve(restarted, folder, 'next-turn').reservation_created


def test_terminal_replay_is_detached_and_cannot_release_a_later_turn_slot(env):
    records, owner, clock, folder = env
    started = running(env)
    usage = {'input_tokens': 10, 'output_tokens': 3, 'cached_input_tokens': 4}
    clock[0] += timedelta(seconds=1)
    first = owner.finish('turn-1', owner_id='user-a', expected_revision=2, status='completed', exit_code=0, usage=usage)
    reserve(owner, folder, 'next-turn')
    slot = records.read('v2_external_run_slots', 'user-a')
    clock[0] += timedelta(seconds=50)
    again = owner.finish('turn-1', owner_id='user-a', expected_revision=started['revision'], status='completed', exit_code=0, usage=usage)
    assert again.changed is False and again.run == first.run
    assert records.read('v2_external_run_slots', 'user-a') == slot
    again.run['usage']['input_tokens'] = 0
    usage['output_tokens'] = 0
    assert owner.read('turn-1', owner_id='user-a')['usage'] == {'input_tokens': 10, 'output_tokens': 3, 'cached_input_tokens': 4}
    with pytest.raises(ExternalRunConflict, match='^external_run_conflicted$'):
        owner.finish('turn-1', owner_id='user-a', expected_revision=3, status='failed', exit_code=7)
    assert records.read('v2_external_run_slots', 'user-a') == slot


def test_two_real_store_workers_cannot_overwrite_conflicting_terminal(env):
    records, _, clock, _ = env
    running(env)
    barrier = threading.Barrier(2)
    def finish(status):
        owner = ExternalRuns(SQLiteStructuredRecordStore(records.database_path), now=lambda: clock[0].isoformat())
        barrier.wait()
        try:
            return owner.finish('turn-1', owner_id='user-a', expected_revision=2, status=status,
                exit_code=0 if status == 'completed' else 7).changed
        except ExternalRunConflict:
            return 'conflict'
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(finish, ['completed', 'failed']))
    assert results.count(True) == 1 and results.count('conflict') == 1
    assert records.read('v2_external_runs', 'turn-1').revision == 3
    assert records.read('v2_external_run_slots', 'user-a').payload['claims'] == []


@pytest.mark.parametrize('stage', ['reserve', 'start', 'finish'])
def test_actual_sql_failure_rolls_back_run_and_slot_together(env, stage):
    records, owner, _, folder = env
    if stage != 'reserve':
        reserve(owner, folder)
    if stage == 'finish':
        owner.mark_started('turn-1', owner_id='user-a', expected_revision=1)
    before_runs, before_slots = records.list('v2_external_runs'), records.list('v2_external_run_slots')
    with records.begin() as tx:
        tx.connection.execute("CREATE TRIGGER reject_slot BEFORE " + ('INSERT' if stage == 'reserve' else 'UPDATE')
            + " ON crp_structured_records WHEN NEW.collection='v2_external_run_slots' "
            "BEGIN SELECT RAISE(ABORT, 'synthetic-private-database-detail'); END")
        tx.commit()
    with pytest.raises(ExternalRunError, match='^external_run_store_failed$'):
        if stage == 'reserve':
            reserve(owner, folder)
        elif stage == 'start':
            owner.mark_started('turn-1', owner_id='user-a', expected_revision=1)
        else:
            owner.finish('turn-1', owner_id='user-a', expected_revision=2, status='completed', exit_code=0)
    assert records.list('v2_external_runs') == before_runs
    assert records.list('v2_external_run_slots') == before_slots


@pytest.mark.parametrize('usage', [True, {}, {'input_tokens': 1}, {'input_tokens': True, 'output_tokens': 2},
    {'input_tokens': -1, 'output_tokens': 2}, {'input_tokens': float('nan'), 'output_tokens': 2},
    {'input_tokens': 1, 'output_tokens': 2, 'secret': 'synthetic-hidden'}, {'input_tokens': 1, 'output_tokens': 2, 'cost': 1.0}])
def test_unreported_or_malformed_usage_is_rejected_without_any_write(env, usage):
    records, owner, _, _ = env
    running(env)
    before = records.list_all()
    with pytest.raises(ExternalRunError, match='^external_run_invalid$'):
        owner.finish('turn-1', owner_id='user-a', expected_revision=2, status='completed', exit_code=0, usage=usage)
    assert records.list_all() == before


@pytest.mark.parametrize('changes', [{'executor': []}, {'executor': 'other'}, {'adapter_version': 'claude-code@1'},
    {'cli_version': 'raw body'}, {'preset': {}}, {'preset': 'unrestricted'}, {'workspace': Path('relative')},
    {'adapter_version': 'sk-' + 'Q' * 24}, {'cli_version': '1.0.0+sk-' + 'Q' * 24},
    {'env': {'secret': 'synthetic-hidden'}}])
def test_metadata_whitelist_rejects_private_or_unknown_input_before_persist(env, changes):
    records, owner, _, folder = env
    with pytest.raises(ExternalRunError, match='^external_run_invalid$'):
        reserve(owner, folder, **changes)
    assert records.list('v2_external_runs') == () and records.list('v2_external_run_slots') == ()


@pytest.mark.parametrize('change', [{'expected_revision': True}, {'exit_code': True}, {'status': []},
    {'rawbody': 'synthetic-private-body'}, {'tail': ['synthetic-private-body']}, {'env': {'secret': 'synthetic-hidden'}}])
def test_terminal_schema_never_accepts_raw_body_environment_tail_or_boolean_numbers(env, change):
    records, owner, _, _ = env
    running(env)
    values = dict(owner_id='user-a', expected_revision=2, status='completed', exit_code=0)
    values.update(change)
    before = records.list_all()
    with pytest.raises(ExternalRunError, match='^external_run_invalid$'):
        owner.finish('turn-1', **values)
    assert records.list_all() == before


@pytest.mark.parametrize('started', [False, True])
def test_clock_reversal_cannot_publish_terminal_or_release_slot(env, started):
    records, owner, clock, folder = env
    saved = running(env) if started else reserve(owner, folder).run
    clock[0] -= timedelta(seconds=1)
    before = records.list_all()
    with pytest.raises(ExternalRunError, match='^external_run_clock_invalid$'):
        owner.finish('turn-1', owner_id='user-a', expected_revision=saved['revision'], status='cancelled', exit_code=None)
    assert records.list_all() == before


def test_aware_clock_normalizes_to_utc_and_naive_clock_is_rejected(env):
    records, _, _, folder = env
    owner = ExternalRuns(records, now=lambda: '2026-10-06T16:00:00+08:00')
    assert reserve(owner, folder).run['reserved_at'] == '2026-10-06T08:00:00+00:00'
    naive = ExternalRuns(records, now=lambda: '2026-10-06T08:00:00')
    with pytest.raises(ExternalRunError, match='^external_run_clock_invalid$'):
        reserve(naive, folder, 'other-turn', 'user-b')
    assert records.read('v2_external_runs', 'other-turn') is None


def test_two_real_store_workers_replay_identical_terminal_without_second_release(env):
    records, _, clock, _ = env
    running(env)
    barrier = threading.Barrier(2)
    def finish(_):
        owner = ExternalRuns(SQLiteStructuredRecordStore(records.database_path), now=lambda: clock[0].isoformat())
        barrier.wait()
        return owner.finish('turn-1', owner_id='user-a', expected_revision=2,
            status='completed', exit_code=0, usage={'input_tokens': 0, 'output_tokens': 0})
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(finish, range(2)))
    assert sorted(result.changed for result in results) == [False, True]
    assert results[0].run == results[1].run
    assert results[0].run['usage'] == {'input_tokens': 0, 'output_tokens': 0}
    assert records.read('v2_external_runs', 'turn-1').revision == 3
    assert records.read('v2_external_run_slots', 'user-a').revision == 3
    assert records.read('v2_external_run_slots', 'user-a').payload['claims'] == []


def test_finish_stale_revision_cannot_end_a_running_cli_or_release_its_slot(env):
    records, owner, _, _ = env
    running(env)
    before = records.list_all()
    with pytest.raises(ExternalRunConflict, match='^external_run_conflicted$'):
        owner.finish('turn-1', owner_id='user-a', expected_revision=1, status='cancelled', exit_code=None)
    assert records.list_all() == before


@pytest.mark.parametrize('missing', [False, True])
def test_missing_or_bad_revision_slot_cannot_grant_a_new_launch_or_release(env, missing):
    records, owner, _, folder = env
    reserve(owner, folder)
    with records.begin() as tx:
        slot = tx.read('v2_external_run_slots', 'user-a')
        if missing:
            tx.delete('v2_external_run_slots', 'user-a', expected_revision=slot.revision)
        else:
            tx.put('v2_external_run_slots', 'user-a', {'owner_id': 'user-a',
                'claims': [{'turn_id': 'turn-1', 'revision': 2}]}, expected_revision=slot.revision)
        tx.commit()
    before = records.list_all()
    with pytest.raises(ExternalRunConflict, match='^external_run_conflicted$'):
        reserve(owner, folder, 'next-turn')
    with pytest.raises(ExternalRunConflict, match='^external_run_conflicted$'):
        owner.finish('turn-1', owner_id='user-a', expected_revision=1, status='cancelled', exit_code=None)
    assert records.list_all() == before


def test_same_turn_binding_cannot_be_changed_even_after_terminal(env):
    records, owner, _, folder = env
    started = running(env)
    final = owner.finish('turn-1', owner_id='user-a', expected_revision=started['revision'], status='completed', exit_code=0)
    before = records.list_all()
    assert reserve(owner, folder).reservation_created is False
    for change in ({'cli_version': '0.157.0'}, {'preset': 'research'}, {'workspace': folder / 'different'}):
        with pytest.raises(ExternalRunConflict, match='^external_run_conflicted$'):
            reserve(owner, folder, **change)
    assert owner.read('turn-1', owner_id='user-a') == final.run
    assert records.list_all() == before


def test_other_executor_metadata_and_foreign_unknown_fact_do_not_pollute_owner_slot(env):
    records, owner, _, folder = env
    with records.begin() as tx:
        tx.put('v2_external_runs', 'foreign-unknown', {'owner_id': 'user-b', 'status': 'unknown'}, expected_revision=0)
        tx.commit()
    saved = reserve(owner, folder, executor='claude-code', adapter_version='claude-code@2', cli_version='2.1.77')
    assert saved.reservation_created and saved.run['executor'] == 'claude-code'
    assert saved.run['adapter_version'] == 'claude-code@2' and saved.run['cli_version'] == '2.1.77'
    assert records.read('v2_external_runs', 'foreign-unknown').payload == {'owner_id': 'user-b', 'status': 'unknown'}
    assert records.read('v2_external_run_slots', 'user-a').payload['claims'] == [{'turn_id': 'turn-1', 'revision': 1}]
