"""Read real edit/division writers and explicit synthetic redo predicate facts."""
from datetime import datetime, timedelta, timezone

import pytest

from backend.memory_app.v2.learning_events import events, checkpoint, completed
from core.storage_provider import SQLiteStructuredRecordStore
from tests.memory_app.v2.test_outcome_corrections import edit_env, _save
from tests.memory_app.v2.test_outcome_divisions import division_env, adjust, OLD


COLLECTION = 'v2_outcome_corrections'


def _synthetic_redo():
    # Only reader qualification is under test; this is not a TaskDo producer.
    return {'kind': 'outcome_redo', 'project_id': 'alpha', 'turn_id': 'old-turn',
        'new_turn_id': 'new-turn', 'document_id': 'old-document', 'birth_revision': 1,
        'from_revision': 2, 'before_title': '旧标题', 'before': '旧正文',
        'policy_version': '@1', 'created_at': '2026-10-06T08:00:00+00:00',
        'new_document_id': 'new-document', 'new_birth_revision': 1, 'to_revision': 1,
        'after_title': '新标题', 'after': '新正文',
        'completed_at': '2026-10-06T08:01:00+00:00'}


def _put(records, identity, payload, *, collection=COLLECTION):
    with records.begin() as tx:
        previous = tx.read(collection, identity)
        saved = tx.put(collection, identity, payload,
            expected_revision=previous.revision if previous else 0)
        tx.commit()
    return saved


def test_actual_http_mature_edit_is_one_namespaced_point(edit_env):
    edit_env[4].value = datetime.now(timezone.utc) - timedelta(seconds=601)
    assert _save(edit_env, '新的做法\n\n保留的段落', 1).status_code == 200
    event, = edit_env[1].list(COLLECTION)
    assert event.payload['net_change'] is True
    assert events(edit_env[1]) == {'project-a': {'outcome:' + event.object_id}}


def test_actual_http_successful_division_is_one_point(division_env):
    assert adjust(division_env).status_code == 200
    event, = division_env.records.list(COLLECTION)
    assert event.payload['division_from_revision'] == 1 and event.payload['division_to_revision'] == 2
    assert events(division_env.records) == {'project-a': {'outcome:' + event.object_id}}


def test_synthetic_completed_redo_shape_is_one_reader_point(tmp_path):
    records = SQLiteStructuredRecordStore(tmp_path / 'facts.sqlite3')
    event = _put(records, 'synthetic-redo', _synthetic_redo())
    assert event.revision == 1
    assert events(records) == {'alpha': {'outcome:synthetic-redo'}}


def test_actual_edit_quiet_boundary_unknown_and_backward_clock(edit_env):
    assert _save(edit_env, '新的做法\n\n保留的段落', 1).status_code == 200
    event, = edit_env[1].list(COLLECTION)
    last = edit_env[4].value
    for seconds in (-1, 0, 600, 601):
        selected = events(edit_env[1], now=(last + timedelta(seconds=seconds)).isoformat())
        assert selected == ({'project-a': {'outcome:' + event.object_id}} if seconds == 601 else {})
    for unknown in ('not-a-time', last.replace(tzinfo=None).isoformat()):
        assert events(edit_env[1], now=unknown) == {}
    assert edit_env[1].read(COLLECTION, event.object_id) == event


def test_actual_noop_moves_open_window_but_not_first_before(edit_env):
    clock = edit_env[4]
    assert _save(edit_env, '中间做法\n\n保留的段落', 1).status_code == 200
    first, = edit_env[1].list(COLLECTION)
    clock.advance(590)
    assert _save(edit_env, '中间做法\n\n保留的段落', 2).status_code == 200
    second, = edit_env[1].list(COLLECTION)
    assert second.object_id == first.object_id and second.payload['from_revision'] == 1
    assert second.payload['to_revision'] == 3 and second.payload['last_saved_at'] == clock()
    assert events(edit_env[1], now=(clock.value + timedelta(seconds=600)).isoformat()) == {}
    assert events(edit_env[1], now=(clock.value + timedelta(seconds=601)).isoformat()) == {
        'project-a': {'outcome:' + first.object_id}}
    clock.advance(60)
    assert _save(edit_env, '最终做法\n\n保留的段落', 3).status_code == 200
    last, = edit_env[1].list(COLLECTION)
    assert last.object_id == first.object_id and last.payload['from_revision'] == 1
    assert last.payload['to_revision'] == 4
    assert events(edit_env[1], now=(clock.value + timedelta(seconds=601)).isoformat()) == {
        'project-a': {'outcome:' + first.object_id}}


def test_actual_net_revert_preserves_fact_without_a_point(edit_env):
    from tests.memory_app.v2.test_outcome_corrections import BASE
    assert _save(edit_env, '中间做法\n\n保留的段落', 1).status_code == 200
    edit_env[4].advance(590)
    assert _save(edit_env, BASE, 2).status_code == 200
    event, = edit_env[1].list(COLLECTION)
    assert event.payload['net_change'] is False and event.payload['to_revision'] == 3
    assert events(edit_env[1], now=(edit_env[4].value + timedelta(seconds=601)).isoformat()) == {}
    assert edit_env[1].read(COLLECTION, event.object_id) == event


def test_actual_same_goals_second_success_is_a_second_point(division_env):
    assert adjust(division_env, items=OLD).status_code == 200
    first, = division_env.records.list(COLLECTION)
    assert first.payload['before_goals'] == first.payload['after_goals']
    assert events(division_env.records) == {'project-a': {'outcome:' + first.object_id}}
    assert adjust(division_env, items=OLD, revision=2).status_code == 200
    rows = division_env.records.list(COLLECTION)
    assert len(rows) == 2
    assert events(division_env.records) == {'project-a': {'outcome:' + row.object_id for row in rows}}
    state = checkpoint(division_env.records, 0)['project-a']
    assert state['score'] == 2
    assert checkpoint(division_env.records, 0)['project-a']['score'] == 2


def _synthetic_edit():
    return {'kind': 'outcome_edit', 'project_id': 'alpha', 'turn_id': 'old-turn',
        'document_id': 'old-document', 'birth_revision': 1, 'from_revision': 2,
        'to_revision': 3, 'before': '旧正文', 'after': '新正文', 'net_change': True,
        'policy_version': '@1', 'created_at': '2026-10-06T08:00:00+00:00',
        'last_saved_at': '2026-10-06T08:00:00+00:00'}


def _synthetic_division():
    return {'kind': 'division_adjust', 'project_id': 'alpha', 'turn_id': 'old-turn',
        'before_goals': ['原目标'], 'after_goals': ['新目标'],
        'division_from_revision': 1, 'division_to_revision': 2,
        'created_at': '2026-10-06T08:00:00+00:00'}


@pytest.mark.parametrize('build', [_synthetic_edit, _synthetic_division, _synthetic_redo])
def test_original_namespaces_checkpoint_and_complete_remain_independent(tmp_path, build):
    from tests.memory_app.v2.test_accumulation_trigger import facts
    records = facts(tmp_path, edits=1)
    initial = events(records)
    assert len(initial['alpha']) == 2
    old, = records.list('v2_correction_events')
    payload = build()
    if payload['kind'] == 'outcome_edit':
        # This control uses checkpoint's actual UTC wall, unlike explicit-clock tests.
        stamp = (datetime.now(timezone.utc) - timedelta(seconds=601)).isoformat()
        payload.update(created_at=stamp, last_saved_at=stamp)
    event = _put(records, old.object_id, payload)
    assert events(records) == {'alpha': initial['alpha'] | {'outcome:' + old.object_id}}
    state = checkpoint(records, 0)['alpha']
    assert state['score'] == 3 and len(state['seen_event_ids']) == 3
    assert checkpoint(records, 0)['alpha']['score'] == 3
    completed(records, 'alpha')
    assert checkpoint(records, 0)['alpha']['score'] == 0
    assert events(records) == {'alpha': initial['alpha'] | {'outcome:' + old.object_id}}
    assert records.read(COLLECTION, old.object_id) == event


def test_still_open_event_is_not_seen_or_sealed_by_checkpoint(edit_env):
    # A future external wall sample stays pending; checkpoint cannot close its window.
    edit_env[4].value = datetime.now(timezone.utc) + timedelta(seconds=60)
    assert _save(edit_env, '中间做法\n\n保留的段落', 1).status_code == 200
    first, = edit_env[1].list(COLLECTION)
    assert checkpoint(edit_env[1], 0) == {}
    assert edit_env[1].list('v2_learning_accumulation') == ()
    edit_env[4].advance(590)
    assert _save(edit_env, '最终做法\n\n保留的段落', 2).status_code == 200
    last, = edit_env[1].list(COLLECTION)
    assert last.object_id == first.object_id and last.payload['to_revision'] == 3
    assert events(edit_env[1], now=(edit_env[4].value + timedelta(seconds=601)).isoformat()) == {
        'project-a': {'outcome:' + first.object_id}}


def test_edit_reader_uses_recorded_policy_and_defers_unknown_versions(tmp_path):
    from backend.memory_app.v2 import policies
    from backend.memory_app.v2.policies.outcome_correction import v1
    if '@9915' not in policies._REGISTRY['outcome_correction']:
        def longer_window(request=None, *, operation, gap_seconds=None):
            if operation == 'window':
                return {'merge': gap_seconds is None or gap_seconds <= 1200,
                        'mature': gap_seconds is not None and gap_seconds > 1200}
            return v1(request, operation=operation, gap_seconds=gap_seconds)
        policies.register('outcome_correction', '@9915')(longer_window)
    records = SQLiteStructuredRecordStore(tmp_path / 'facts.sqlite3')
    payload = {**_synthetic_edit(), 'policy_version': '@9915'}
    event = _put(records, 'policy-edit', payload)
    with policies.override(outcome_correction='@1'):
        assert events(records, now='2026-10-06T08:10:01+00:00') == {}
        assert events(records, now='2026-10-06T08:20:01+00:00') == {'alpha': {'outcome:policy-edit'}}
    assert records.read(COLLECTION, event.object_id) == event
    for version in (None, 'unknown', '@999999'):
        _put(records, event.object_id, {**payload, 'policy_version': version})
        assert events(records, now='2026-10-06T10:00:00+00:00') == {}
    assert policies.ACTIVE['outcome_correction'] == '@1'


@pytest.mark.parametrize('field', ['after_title', 'after', 'new_document_id',
    'new_birth_revision', 'to_revision', 'completed_at'])
def test_pending_or_partial_synthetic_redo_never_counts(tmp_path, field):
    records = SQLiteStructuredRecordStore(tmp_path / 'facts.sqlite3')
    payload = _synthetic_redo()
    del payload[field]
    _put(records, 'pending-redo', payload)
    assert events(records) == {}
    assert checkpoint(records, 0) == {}
    assert records.list('v2_learning_accumulation') == ()


def test_synthetic_redo_completion_accepts_empty_after_as_a_real_field(tmp_path):
    records = SQLiteStructuredRecordStore(tmp_path / 'facts.sqlite3')
    _put(records, 'empty-after-redo', {**_synthetic_redo(), 'after': ''})
    assert events(records) == {'alpha': {'outcome:empty-after-redo'}}


@pytest.mark.parametrize('build,changes', [
    (_synthetic_edit, {'net_change': 1}),
    (_synthetic_edit, {'to_revision': True}),
    (_synthetic_edit, {'from_revision': 0}),
    (_synthetic_edit, {'last_saved_at': None}),
    (_synthetic_edit, {'last_saved_at': '2026-10-06T08:00:00'}),
    (_synthetic_edit, {'before': None}),
    (_synthetic_division, {'before_goals': []}),
    (_synthetic_division, {'after_goals': '新目标'}),
    (_synthetic_division, {'after_goals': [' ']}),
    (_synthetic_division, {'division_to_revision': 1}),
    (_synthetic_division, {'division_from_revision': True}),
    (_synthetic_redo, {'completed_at': 'unknown'}),
    (_synthetic_redo, {'new_birth_revision': 2}),
    (_synthetic_redo, {'after_title': None}),
    (_synthetic_redo, {'new_turn_id': 'old-turn'}),
    (_synthetic_redo, {'new_document_id': 'old-document'}),
    (_synthetic_redo, {'project_id': None}),
    (_synthetic_redo, {'turn_id': ''}),
    (_synthetic_redo, {'created_at': None}),
    (_synthetic_redo, {'kind': 'unknown'}),
])
def test_malformed_synthetic_facts_stay_pending_without_writes(tmp_path, build, changes):
    records = SQLiteStructuredRecordStore(tmp_path / 'facts.sqlite3')
    event = _put(records, 'bad-fact', {**build(), **changes})
    assert events(records, now='2026-10-06T08:30:00+00:00') == {}
    assert records.read(COLLECTION, event.object_id) == event


def test_projects_remain_isolated_and_no_outcome_record_is_written_by_readers(tmp_path):
    records = SQLiteStructuredRecordStore(tmp_path / 'facts.sqlite3')
    _put(records, 'alpha-event', _synthetic_redo())
    _put(records, 'beta-event', {**_synthetic_division(), 'project_id': 'beta'})
    before = records.list(COLLECTION)
    assert events(records) == {'alpha': {'outcome:alpha-event'}, 'beta': {'outcome:beta-event'}}
    assert checkpoint(records, 0)['alpha']['score'] == 1
    completed(records, 'alpha')
    states = checkpoint(records, 0)
    assert states['alpha']['score'] == 0 and states['beta']['score'] == 1
    assert records.list(COLLECTION) == before
