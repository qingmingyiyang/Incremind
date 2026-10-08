"""临时 SQLite 运行事实与真实模拟命令生命周期组合。"""
import os
import sys

import pytest

from backend.memory_app.v2.external_events import ExternalEventParser
from backend.memory_app.v2.external_process import run_process
from backend.memory_app.v2.external_runs import ExternalRuns
from core.storage_provider import SQLiteStructuredRecordStore


@pytest.mark.parametrize('mode,status,exit_code,started', [
    ('success', 'completed', 0, True),
    ('protocol_failure', 'failed', 0, True),
    ('crash', 'failed', 7, True),
    ('spawn_failure', 'failed', None, False),
])
def test_start_and_terminal_facts_follow_real_process_and_protocol(tmp_path, mode, status, exit_code, started):
    records = SQLiteStructuredRecordStore(tmp_path / 'records.sqlite3')
    runs = ExternalRuns(records)
    metadata = dict(owner_id='local-user', executor='codex', adapter_version='codex@1',
        cli_version='1.0.0', preset='workspace', workspace=tmp_path)
    reservation = runs.reserve('turn-real', **metadata)
    assert reservation.reservation_created and reservation.run['started_at'] is None
    state = {'run': reservation.run, 'starts': 0}
    parser = ExternalEventParser('codex')

    def began():
        state['run'] = runs.mark_started('turn-real', owner_id='local-user',
            expected_revision=state['run']['revision']).run
        state['starts'] += 1

    kind = 'turn.completed' if mode == 'success' else 'turn.failed'
    body = 'import json,sys; print(json.dumps(' + repr({
        'type': kind, 'usage': {'input_tokens': 17, 'output_tokens': 5}}) + '), flush=True)'
    if mode == 'crash':
        body += '; sys.exit(7)'
    command = ([str(tmp_path / 'missing-executable')] if mode == 'spawn_failure' else
        [sys._base_executable, '-I', '-S', '-u', '-c', body])
    environment = {key: os.environ[key] for key in ('SystemRoot', 'WINDIR') if key in os.environ}
    result = run_process(command, cwd=tmp_path, environment=environment,
        on_started=began, on_line=parser.feed_line)
    terminal = 'failed' if parser.failed else result.status
    assert terminal == status and result.exit_code == exit_code
    assert state['starts'] == int(started)
    completed = runs.finish('turn-real', owner_id='local-user', status=terminal,
        exit_code=result.exit_code, usage=parser.usage, expected_revision=state['run']['revision'])
    assert completed.changed
    actual = runs.read('turn-real', owner_id='local-user')
    assert actual['status'] == status and actual['exit_code'] == exit_code
    assert (actual['started_at'] is not None) is started
    assert actual['ended_at'] is not None
    assert actual['usage'] == ({'input_tokens': 17, 'output_tokens': 5} if started else None)
    # 真实 owner 已清理并且终态事务提交以后，下一项才取得唯一运行槽。
    next_run = runs.reserve('turn-next', **metadata)
    assert next_run.reservation_created
    assert next_run.run['status'] == 'reserved' and next_run.run['started_at'] is None
