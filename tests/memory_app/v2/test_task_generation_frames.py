"""Hard process death retains derived task text without new execution."""
import json
import os
from copy import deepcopy
from pathlib import Path
import subprocess
import sys
import time
import sqlite3
from threading import Event
from uuid import uuid4

from fastapi import FastAPI
from fastapi.testclient import TestClient
import pytest

from backend.memory_app.model_config import ModelConfiguration
from backend.memory_app.storage_authority import resolve_recognition_document_store
from backend.security.secrets import InMemorySecretStore
from tests.memory_app.v2.test_workbench_do import env


COMPLETE = '已经完成的任务段落。' + '合成正文' * 110 + '。\n\n'


def task_crash_driver(root):
    root = Path(root)
    (root / 'config').mkdir(exist_ok=True)
    (root / 'config/settings.toml').write_bytes(
        (Path(__file__).parents[3] / 'config/settings.toml.example').read_bytes())
    records, _ = resolve_recognition_document_store(root)
    def provider(**request):
        context = json.loads(next(message['content'] for message in reversed(request['messages'])
            if message['role'] == 'user' and message['content'].startswith('{')))
        if 'output' in context:
            return {'choices': [{'message': {'content': json.dumps({'mode': 'main_only', 'assignments': []})},
                                 'finish_reason': 'stop'}],
                    'usage': {'prompt_tokens': 5, 'completion_tokens': 3}}
        (root / 'task-wire-count.txt').write_text('1')
        def stream():
            yield {'choices': [{'delta': {'content': '{"type":"complete","summary":' +
                json.dumps(COMPLETE + '尚未完成的一段', ensure_ascii=False)[:-1]}, 'finish_reason': None}],
                'usage': {'prompt_tokens': 5, 'completion_tokens': 3}}
            product = records.list('v2_turns')[0]
            execution = records.read('v2_task_executions', product.object_id)
            (root / 'task-ready.json').write_text(json.dumps({'turn_id': product.object_id,
                'thread_id': product.payload['thread_id'], 'kernel_turn_id': execution.payload['request']['turn_id']}))
            Event().wait()
        return stream()
    models = ModelConfiguration(records, root, InMemorySecretStore(), completion_fn=provider)
    models.update('generation', {'base_url': 'https://example.test', 'model': 'test-model',
        'api_key': 'synthetic-only', 'allow_remote': True, 'expected_revision': 0})
    import importlib
    importlib.import_module('litellm')
    from backend.memory_app.app import create_app
    with TestClient(create_app(runtime_root=root, legacy_app=FastAPI(), model_configuration=models)) as http:
        response = http.post('/api/v2/workbench/turns', json={'project_id': 'project-a',
            'intent': 'do', 'text': '写出完整文字成果'})
        assert response.status_code == 200, response.text
        Event().wait()


def test_task_summary_hard_kill_reopens_bound_frames_without_start_or_wire(tmp_path, monkeypatch):
    # Keep actual bundled capability materialization within Windows path limits.
    root = Path(__file__).parents[3] / 'work' / ('T14.5-fk-' + uuid4().hex[:8])
    root.mkdir()
    child_env = {**os.environ, 'CHRIPTMAS_APP_ROOT': str(root),
        'PYTHONPATH': str(Path(__file__).parents[3] / 'src') + os.pathsep + os.environ.get('PYTHONPATH', '')}
    with (root / 'task-child-output.log').open('w') as output:
        child = subprocess.Popen([sys.executable, '-c',
            'from tests.memory_app.v2.test_task_generation_frames import task_crash_driver; '
            'import sys; task_crash_driver(sys.argv[1])', str(root)],
            env=child_env, stdout=output, stderr=subprocess.STDOUT)
        try:
            deadline = time.monotonic() + 45
            while not (root / 'task-ready.json').is_file() and time.monotonic() < deadline and child.poll() is None:
                time.sleep(.03)
            assert (root / 'task-ready.json').is_file(), (root / 'task-child-output.log').read_text()
            saved = json.loads((root / 'task-ready.json').read_text())
            child.kill()
            child.wait(timeout=10)
        finally:
            if child.poll() is None:
                child.kill()
                child.wait(timeout=10)
    assert (root / 'task-wire-count.txt').read_text() == '1'
    # The provider marker overwrites its file; the kernel is the exact count.
    database = root / '.rebuild-data/ai-turns.sqlite3'
    assert database.is_file()
    with sqlite3.connect(database.resolve().as_uri() + '?mode=ro', uri=True) as connection:
        kernel_events = [json.loads(row[0]) for row in connection.execute(
            'SELECT event_json FROM ai_turn_events WHERE turn_id=? ORDER BY sequence',
            (saved['kernel_turn_id'],))]
        dispatches = [event for event in kernel_events if event['type'] == 'model.attempt.dispatched']
        reservations = connection.execute(
            'SELECT attempt_id,status,terminal_receipt_ref FROM ai_model_attempt_reservations WHERE turn_id=?',
            (saved['kernel_turn_id'],)).fetchall()
    assert len(dispatches) == len(reservations) == 1
    attempt_id, reservation_status, terminal_ref = reservations[0]
    assert reservation_status == 'committed' and terminal_ref is None
    assert not any(event['type'] == 'model.attempt.terminal' for event in kernel_events)
    from core.effect_log import EffectLog, EffectReaper, EffectState
    effects = EffectLog(database)
    killed = effects.get(attempt_id)
    assert killed.turn_id == saved['kernel_turn_id'] and killed.state is EffectState.INFLIGHT
    recovered = EffectReaper(effects).recover_expired(now=int(killed.lease_expires_at) + 1)
    assert [outcome.operation_id for outcome in recovered] == [attempt_id]
    assert effects.get(attempt_id).state is EffectState.UNKNOWN
    records, _ = resolve_recognition_document_store(root)
    before_product = records.read('v2_turns', saved['turn_id'])
    before_execution = records.read('v2_task_executions', saved['turn_id'])
    frame = records.read('v2_turn_frames', saved['turn_id'])
    assert frame is not None
    assert frame.payload['project_id'] == 'project-a' and frame.payload['thread_id'] == saved['thread_id']
    assert frame.payload['turn']['id'] == saved['turn_id'] and frame.payload['turn']['intent'] == 'do'
    assert ''.join(value['text'] for value in frame.payload['frames']) == COMPLETE + '尚未完成的一段'
    calls = []
    def unexpected(**request):
        calls.append(request)
        raise AssertionError('a restarted task frame is read-only')
    configured = records.read('recognition_model_config', 'generation')
    secrets = InMemorySecretStore({configured.payload['secret_ref']: 'synthetic-only'})
    models = ModelConfiguration(records, root, secrets, completion_fn=unexpected)
    assert models.public()['generation']['revision'] == 1 and models.public()['generation']['has_api_key']
    monkeypatch.setenv('CHRIPTMAS_APP_ROOT', str(root))
    from backend.memory_app.app import create_app
    with TestClient(create_app(runtime_root=root, legacy_app=FastAPI(), model_configuration=models)) as http:
        response = http.get(f"/api/v2/workbench/threads/{saved['thread_id']}?project_id=project-a")
        assert response.status_code == 200, response.text
        turn = response.json()['turns'][0]
        assert turn['id'] == saved['turn_id']
        assert turn['receipt']['do']['state'] == 'interrupted'
        assert turn['receipt']['do']['partial'] == COMPLETE
        assert turn['receipt']['do']['interruption'] == 'connection'
        assert turn['receipt']['do']['document_id'] is None
        assert records.read('v2_task_continuations', saved['kernel_turn_id']) is None
        assert records.read('v2_turns', saved['turn_id']) == before_product
        assert records.read('v2_task_executions', saved['turn_id']) == before_execution
        repeat = http.get(f"/api/v2/workbench/threads/{saved['thread_id']}?project_id=project-a")
        assert repeat.json() == response.json()
        assert http.get(f"/api/v2/workbench/threads/{saved['thread_id']}?project_id=other").status_code == 404
        rejected = http.post(f"/api/v2/workbench/turns/{saved['turn_id']}/continue",
            json={'project_id': 'project-a'}, headers={'Idempotency-Key': 'killed-without-close-witness'})
        assert rejected.status_code == 409, rejected.text
        assert records.read('v2_task_continuations', saved['kernel_turn_id']) is None
        bad_frames = []
        for key, value in [('project_id', 'other'), ('thread_id', 'thread-forged'),
                           ('text_from', True), ('text_prefix', 42)]:
            bad_frames.append({**frame.payload, key: value})
        forged = deepcopy(frame.payload)
        forged['projection']['kernel_turn_id'] = 'turn-forged'
        bad_frames.append(forged)
        forged = deepcopy(frame.payload)
        forged['turn']['user_text'] = 'forged task'
        bad_frames.append(forged)
        forged = deepcopy(frame.payload)
        forged['frames'][0]['sequence'] = 2
        bad_frames.append(forged)
        for payload in bad_frames:
            with records.begin() as tx:
                current = tx.read('v2_turn_frames', saved['turn_id'])
                tx.put('v2_turn_frames', saved['turn_id'], payload, expected_revision=current.revision)
                tx.commit()
            frozen_before = records.read('v2_turn_frames', saved['turn_id'])
            read = http.get(f"/api/v2/workbench/threads/{saved['thread_id']}?project_id=project-a")
            assert read.status_code == 200, read.text
            actual = read.json()['turns'][0]['receipt']['do']
            assert actual['state'] == before_product.payload['receipt']['do']['state']
            assert 'partial' not in actual and 'interruption' not in actual
            assert records.read('v2_turn_frames', saved['turn_id']) == frozen_before
            assert records.read('v2_turns', saved['turn_id']) == before_product
            assert records.read('v2_task_executions', saved['turn_id']) == before_execution
        with records.begin() as tx:
            current = tx.read('v2_turn_frames', saved['turn_id'])
            tx.delete('v2_turn_frames', saved['turn_id'], expected_revision=current.revision)
            tx.commit()
        missing = http.get(f"/api/v2/workbench/threads/{saved['thread_id']}?project_id=project-a")
        assert missing.status_code == 200 and 'partial' not in missing.json()['turns'][0]['receipt']['do']
        assert records.read('v2_turns', saved['turn_id']) == before_product
        assert records.read('v2_task_executions', saved['turn_id']) == before_execution
    assert calls == []
    with sqlite3.connect(database.resolve().as_uri() + '?mode=ro', uri=True) as connection:
        after_events = [json.loads(row[0]) for row in connection.execute(
            'SELECT event_json FROM ai_turn_events WHERE turn_id=? ORDER BY sequence',
            (saved['kernel_turn_id'],))]
    assert [event for event in after_events if event['type'] == 'model.attempt.dispatched'] == dispatches
    assert not any(event['type'] == 'model.attempt.terminal' for event in after_events)
    assert effects.get(attempt_id).state is EffectState.UNKNOWN
    print('TASK_KILL_AUTHORITY: exact dispatches=1; committed/no terminal; '
          'original Effect INFLIGHT -> Core Reaper UNKNOWN; newapp/GET/continue new wires=0')


def test_task_frame_read_request_is_exact_readonly_and_rejects_conflict_and_wrong_kind(tmp_path):
    from backend.memory_app.kernel.receipt_projection import frozen_task_request
    from core.ai_kernel.sqlite_store import SQLiteAITurnStore
    from tests.rebuild.test_product_turn_kinds import request
    root = tmp_path / 'metadata-only'
    identity = 'turn-' + 'a' * 32
    assert frozen_task_request(root, identity, 'alpha', question='synthetic input') is None
    assert not root.exists()
    value = request('project.task', project_id='alpha')
    database = root / '.rebuild-data/ai-turns.sqlite3'
    SQLiteAITurnStore(database).claim_turn(value)
    before = database.stat().st_mtime_ns
    assert frozen_task_request(root, identity, 'alpha', question='synthetic input') == value
    assert database.stat().st_mtime_ns == before
    assert frozen_task_request(root, identity, 'other', question='synthetic input') is None
    assert frozen_task_request(root, identity, 'alpha', question='forged input') is None
    assert not (root / 'ai-turns.sqlite3').exists()
    with sqlite3.connect(database) as connection:
        connection.execute('UPDATE ai_turns SET request_json=? WHERE turn_id=?',
            (json.dumps(request('project.answer', project_id='alpha', template_version=2,
                                capabilities=['workbench.answer.execute'])), identity))
    assert frozen_task_request(root, identity, 'alpha', question='synthetic input') is None
    with sqlite3.connect(database) as connection:
        connection.execute('UPDATE ai_turns SET request_json=? WHERE turn_id=?', (json.dumps(value), identity))
    SQLiteAITurnStore(root / 'ai-turns.sqlite3').claim_turn({**value, 'created_at': '2026-10-03T00:00:00Z'})
    assert frozen_task_request(root, identity, 'alpha', question='synthetic input') is None


def test_task_frame_continuation_preserves_sequence_and_replaces_discarded_tail(tmp_path):
    from backend.memory_app.v2.turn_frames import TurnFrames, frame_text
    from backend.memory_app.v2.policies import get
    from core.storage_provider import SQLiteStructuredRecordStore
    records = SQLiteStructuredRecordStore(tmp_path / 'records.sqlite3')
    options = dict(turn_id='turn-frame-segment', project_id='alpha', recipe=get('retry', version='@1'),
                   projection={'kernel_turn_id': 'turn-kernel', 'request': {}}, schedule=False)
    first = TurnFrames(records, **options, text_prefix='')
    first.delta('完整段落。\n\n需要丢弃的旧尾段')
    first.close(completed=False)
    second = TurnFrames(records, **options, text_prefix='完整段落。\n\n')
    second.delta('后续段落。\n\n新的尾段')
    second.close(completed=False)
    row = records.read('v2_turn_frames', 'turn-frame-segment')
    assert [value['sequence'] for value in row.payload['frames']] == [1, 2]
    assert row.payload['text_from'] == 2
    assert frame_text(row.payload) == '完整段落。\n\n后续段落。\n\n新的尾段'
    assert first.timer is second.timer is None
    second.close(completed=True)
    assert records.read('v2_turn_frames', 'turn-frame-segment') is None


@pytest.mark.parametrize('frame_failure', [False, True])
def test_real_main_summary_finishes_when_frames_work_or_sqlite_rejects_projection(frame_failure, env):
    client, models = env
    records = client.app.state.recognition_service.records
    if frame_failure:
        with sqlite3.connect(records.database_path) as connection:
            connection.execute("CREATE TRIGGER reject_task_frames BEFORE INSERT ON crp_structured_records "
                "WHEN NEW.collection='v2_turn_frames' BEGIN SELECT RAISE(ABORT,'synthetic derived frame failure'); END")
    calls, closed, observed = [], [], []
    def provider(**request):
        context = json.loads(next(message['content'] for message in reversed(request['messages'])
            if message['role'] == 'user' and message['content'].startswith('{')))
        if 'output' in context:
            return {'choices': [{'message': {'content': json.dumps({'mode': 'main_only', 'assignments': []})},
                                 'finish_reason': 'stop'}], 'usage': {'prompt_tokens': 5, 'completion_tokens': 3}}
        calls.append(request)
        def stream():
            try:
                output = json.dumps({'type': 'complete', 'summary': COMPLETE}, ensure_ascii=False)
                yield {'choices': [{'delta': {'content': output}, 'finish_reason': None}]}
                observed.append(len(records.list('v2_turn_frames')))
                yield {'choices': [{'delta': {}, 'finish_reason': 'stop'}],
                       'usage': {'prompt_tokens': 7, 'completion_tokens': 4}}
            finally:
                closed.append(True)
        return stream()
    models._completion_fn = provider
    created = client.post('/api/v2/workbench/turns', json={'project_id': 'project-a', 'intent': 'do',
                                                       'text': '完成完整长段文字成果'})
    assert created.status_code == 200, created.text
    runtime, store = client.app.state.ai_runtime, client.app.state.ai_turn_store
    deadline = time.monotonic() + 40
    main = None
    while time.monotonic() < deadline:
        main = next((run for run in client.app.state.agent_runtime_composition.store.list_runs(project_id='project-a')
                     if run.role == 'main'), None)
        if main and store.events_after(main.turn_id):
            status = runtime.receipt_for(main.turn_id).status
            if status in {'completed', 'failed'} and main.turn_id not in client.app.state.ai_turn_runner.active_turn_ids:
                break
        time.sleep(.05)
    assert main is not None and status == 'completed'
    assert len(calls) == 1 and closed == [True]
    assert observed == [0 if frame_failure else 1]
    assert records.list('v2_turn_frames') == ()
    attempts = [store.get(row['data']['receipt_ref']) for row in store.events_after(main.turn_id)
                if row['type'] == 'model.attempt.terminal']
    assert len(attempts) == 1 and attempts[0]['status'] == 'succeeded'
