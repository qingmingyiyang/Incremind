"""Derived stream frames use the real record CAS; no frame grants execution."""
import sqlite3
from time import monotonic, sleep

import pytest

from core.storage_provider import SQLiteStructuredRecordStore
from backend.memory_app.v2.policies import get


def test_frozen_partial_recipe_drops_open_paragraph_and_detects_sleep():
    recipe = get('retry', version='@1')
    assert recipe({'kind': 'partial_limits'}) == {
        'frame_characters': 400, 'frame_seconds': 1.0, 'sleep_skew_seconds': 30.0,
        'text_continuations': 3}
    text = '第一段。\n\n```python\nunfinished\n\n'
    assert recipe({'kind': 'partial_output', 'text': text, 'wall_elapsed': 40, 'monotonic_elapsed': 1}) == {
        'partial': '第一段。\n\n', 'interruption': 'sleep'}
    assert recipe({'kind': 'partial_output', 'text': 'incomplete', 'wall_elapsed': 2, 'monotonic_elapsed': 2}) == {
        'partial': '', 'interruption': 'connection'}
    assert recipe({'kind': 'partial_output', 'text': '段落。\r\n\r\n尾', 'wall_elapsed': 31, 'monotonic_elapsed': 1}) == {
        'partial': '段落。\r\n\r\n', 'interruption': 'connection'}


def test_frames_flush_at_character_and_time_boundaries_and_reopen_without_dispatch(tmp_path):
    from backend.memory_app.v2.turn_frames import TurnFrames
    path = tmp_path / 'records.sqlite3'
    records = SQLiteStructuredRecordStore(path)
    clock = [0.0]
    frames = TurnFrames(records, turn_id='turn-a', project_id='alpha',
        recipe=get('retry', version='@1'), clock=lambda: clock[0], schedule=False)
    frames.delta('a' * 399)
    assert records.read('v2_turn_frames', 'turn-a') is None
    frames.delta('b')
    row = records.read('v2_turn_frames', 'turn-a')
    assert row.revision == 1
    assert row.payload['frames'] == [{'sequence': 1, 'text': 'a' * 399 + 'b'}]
    frames.delta('未完成')
    clock[0] = 1
    frames.flush_due()
    reopened = SQLiteStructuredRecordStore(path).read('v2_turn_frames', 'turn-a')
    assert reopened.revision == 2
    assert reopened.payload['frames'][-1] == {'sequence': 2, 'text': '未完成'}
    frames.close(completed=False)
    assert reopened.payload['project_id'] == 'alpha'
    frames.close(completed=True)
    assert records.read('v2_turn_frames', 'turn-a') is None


@pytest.mark.parametrize('unsafe', [
    {'reason': 'provider-text'}, {'used': True}, {'used': 11}, {'limit': 1.5},
    {'delay': True}, {'delay': float('nan')}, {'attempt': False}, {'attempt': 2 ** 53},
    {'body': 'synthetic private text'},
])
def test_retry_frame_rejects_unsafe_metadata_without_creating_a_head(tmp_path, unsafe):
    from backend.memory_app.v2.turn_frames import TurnFrames
    records = SQLiteStructuredRecordStore(tmp_path / 'records.sqlite3')
    frame = TurnFrames(records, turn_id='turn-a', project_id='alpha', recipe=get('retry', version='@1'),
        turn={'id': 'turn-a', 'thread_id': 'thread-a', 'intent': 'ask', 'user_text': 'synthetic', 'created_at': 'now'},
        request={'turn_id': 'turn-a'}, schedule=False)
    frame.retry({'attempt': 1, 'delay': 1.0, 'budget': 'before_output',
        'reason': 'server', 'used': 1, 'limit': 10, **unsafe})
    assert records.read('v2_turn_streams', 'turn-a') is None
    assert records.read('v2_turn_frames', 'turn-a') is None


def test_retry_frame_commits_once_and_clears_on_actual_text_with_safe_server_time(tmp_path):
    from backend.memory_app.v2.turn_frames import TurnFrames
    records = SQLiteStructuredRecordStore(tmp_path / 'records.sqlite3')
    delivered = []
    frame = TurnFrames(records, turn_id='turn-a', project_id='alpha', recipe=get('retry', version='@1'),
        turn={'id': 'turn-a', 'thread_id': 'thread-a', 'intent': 'ask', 'user_text': 'synthetic', 'created_at': 'now'},
        request={'turn_id': 'turn-a'}, schedule=False, wall_clock=lambda: 100,
        on_frame=lambda *value: delivered.append(value))
    frame.retry({'attempt': 1, 'delay': 2.0, 'budget': 'before_output', 'reason': 'server', 'used': 1, 'limit': 10})
    head = records.read('v2_turn_streams', 'turn-a')
    assert head.payload['sequence'] == 2
    assert records.read('v2_turn_frames', 'turn-a') is None
    assert delivered[-1] == (2, 'status', {'state': 'retrying', 'retry': {
        'attempt': 1, 'delay': 2.0, 'budget': 'before_output', 'reason': 'server',
        'used': 1, 'limit': 10, 'retry_at': 102.0}, 'server_time': 100})
    frame.flush_due()
    assert records.read('v2_turn_streams', 'turn-a') == head
    frame.delta('真实正文')
    assert delivered[-1] == (3, 'status', {'state': 'running'})
    assert records.read('v2_turn_streams', 'turn-a').payload['events'][-1] == {
        'sequence': 3, 'kind': 'status', 'state': 'running'}
    frame.close(completed=False)


def test_continuation_retry_does_not_reset_previous_terminal_or_prefix(tmp_path):
    from backend.memory_app.v2.turn_frames import TurnFrames
    records = SQLiteStructuredRecordStore(tmp_path / 'records.sqlite3')
    turn = {'id': 'turn-a', 'thread_id': 'thread-a', 'intent': 'ask', 'user_text': 'synthetic', 'created_at': 'now'}
    saved = {'project_id': 'alpha', 'thread_id': 'thread-a', 'turn': turn,
        'binding': {'kernel_turn_id': 'turn-a', 'request': {'turn_id': 'turn-a'}},
        'sequence': 3, 'events': [{'sequence': 1, 'kind': 'started'},
            {'sequence': 2, 'kind': 'status', 'state': 'interrupted'}, {'sequence': 3, 'kind': 'done'}],
        'terminal': 3}
    with records.begin() as tx:
        tx.put('v2_turn_streams', 'turn-a', saved, expected_revision=0)
        tx.put('v2_turn_frames', 'turn-a', {'project_id': 'alpha', 'thread_id': 'thread-a',
            'turn': turn, 'frames': [{'sequence': 1, 'text': '旧完整段落。\n\n'}]}, expected_revision=0)
        tx.commit()
    before = {name: records.read(name, 'turn-a') for name in ('v2_turn_streams', 'v2_turn_frames')}
    frame = TurnFrames(records, turn_id='turn-a', project_id='alpha', recipe=get('retry', version='@1'),
        turn=turn, request={'turn_id': 'turn-a'}, text_prefix='旧完整段落。\n\n', schedule=False)
    frame.retry({'attempt': 1, 'delay': 1.0, 'budget': 'before_output', 'reason': 'server', 'used': 1, 'limit': 10})
    assert {name: records.read(name, 'turn-a') for name in before} == before


def test_frame_sqlite_write_failure_does_not_drop_delivered_text_or_raise(tmp_path):
    from backend.memory_app.v2.turn_frames import TurnFrames
    records = SQLiteStructuredRecordStore(tmp_path / 'records.sqlite3')
    with records.begin() as tx:
        tx.put('synthetic_records', 'unrelated', {'value': 'unchanged'}, expected_revision=0)
        tx.commit()
    with sqlite3.connect(records.database_path) as connection:
        connection.execute("CREATE TRIGGER reject_frame BEFORE INSERT ON crp_structured_records "
            "WHEN NEW.collection='v2_turn_frames' BEGIN SELECT RAISE(ABORT,'synthetic frame failure'); END")
    frame = TurnFrames(records, turn_id='turn-a', project_id='alpha',
        recipe=get('retry', version='@1'), schedule=False)
    frame.delta('x' * 400)
    frame.close(completed=False)
    assert frame.text == 'x' * 400
    assert records.read('v2_turn_frames', 'turn-a') is None


def test_real_timer_flushes_a_short_delta_and_close_joins_its_thread(tmp_path):
    from backend.memory_app.v2.turn_frames import TurnFrames
    records = SQLiteStructuredRecordStore(tmp_path / 'records.sqlite3')
    frame = TurnFrames(records, turn_id='turn-a', project_id='alpha', recipe=get('retry', version='@1'))
    frame.delta('不足400字的正文')
    timer = frame.timer
    deadline = monotonic() + 4
    while records.read('v2_turn_frames', 'turn-a') is None and monotonic() < deadline:
        sleep(.02)
    assert records.read('v2_turn_frames', 'turn-a').payload['frames'] == [
        {'sequence': 1, 'text': '不足400字的正文'}]
    frame.close(completed=False)
    assert not timer.is_alive()


def test_frozen_frame_request_is_readonly_and_rejects_ambiguous_database_identity(tmp_path):
    from backend.memory_app.kernel.receipt_projection import frozen_answer_request
    from core.ai_kernel.sqlite_store import SQLiteAITurnStore
    from tests.rebuild.test_product_turn_kinds import request
    root = tmp_path / 'runtime'
    identity = 'turn-' + 'a' * 32
    read = lambda **values: frozen_answer_request(root, identity, 'alpha', question='synthetic input', **values)
    assert read() is None and not root.exists()
    first = request('project.answer', project_id='alpha', template_version=2,
        capabilities=['workbench.answer.execute'])
    main = root / '.rebuild-data/ai-turns.sqlite3'
    SQLiteAITurnStore(main).claim_turn(first)
    before = main.stat().st_mtime_ns
    assert read() == first
    assert main.stat().st_mtime_ns == before
    assert not (root / 'ai-turns.sqlite3').exists()
    assert frozen_answer_request(root, identity, 'other', question='synthetic input') is None
    assert frozen_answer_request(root, identity, 'alpha', question='forged question') is None
    assert frozen_answer_request(root, identity, 'alpha', question=None) is None
    second = {**first, 'created_at': '2026-10-03T00:00:00Z'}
    SQLiteAITurnStore(root / 'ai-turns.sqlite3').claim_turn(second)
    assert read() is None


@pytest.mark.parametrize('kind', ['project.task', 'malformed'])
def test_frame_request_rejects_wrong_kind_and_malformed_persisted_request(tmp_path, kind):
    import json
    from backend.memory_app.kernel.receipt_projection import frozen_answer_request
    from core.ai_kernel.sqlite_store import SQLiteAITurnStore
    from tests.rebuild.test_product_turn_kinds import request
    value = request('project.task', project_id='alpha')
    database = tmp_path / 'ai-turns.sqlite3'
    SQLiteAITurnStore(database).claim_turn(value)
    if kind == 'malformed':
        with sqlite3.connect(database) as connection:
            connection.execute('UPDATE ai_turns SET request_json=?', (json.dumps({'turn_id': value['turn_id']}),))
    assert frozen_answer_request(tmp_path, value['turn_id'], 'alpha', question='synthetic input') is None
