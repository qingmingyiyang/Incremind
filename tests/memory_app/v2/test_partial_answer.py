"""Partial output through the real product Turn, configuration and SQLite owners."""
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import time
from types import SimpleNamespace

import pytest

from fastapi.testclient import TestClient

from backend.memory_app.model_config import ModelConfiguration
from backend.memory_app.v2.followup import read_history
from backend.security.secrets import InMemorySecretStore
from tests.memory_app.v2.test_workbench_ask import env, assemble, publish
from tests.memory_app.v2.test_workbench_stream import events


def interrupted_app(state, *, revoke=False, continuation=False, close_failure=False, invalid_citations=False,
                    history_reply=False):
    calls, closed = [], []
    complete = '已经完成的第一段 [1]。\n\n已经完成的第二段。\n\n'
    raw = '{"answer":' + json.dumps(complete + '尚未完成的一段', ensure_ascii=False)[:-1]

    def completion(**request):
        calls.append(request)
        if history_reply and request.get('stream') is not True:
            system = '\n'.join(message['content'] for message in request['messages'] if message['role'] == 'system')
            if 'condensed_question' in system:
                reply = {'condensed_question': 'alpha?'}
            elif 'queries' in system:
                reply = {'queries': []}
            else:
                raise AssertionError('unexpected synthetic auxiliary protocol')
            return {'choices': [{'message': {'content': json.dumps(reply)},
                                 'finish_reason': 'stop'}],
                    'usage': {'prompt_tokens': 3, 'completion_tokens': 1}}
        if close_failure:
            class UnclosedProvider:
                sent = False
                def __iter__(self):
                    return self
                def __next__(self):
                    if self.sent:
                        raise ConnectionError('synthetic disconnected provider')
                    self.sent = True
                    return {'choices': [{'delta': {'content': raw}, 'finish_reason': None}]}
                def close(self):
                    closed.append(False)
                    raise OSError('synthetic close failed')
            return UnclosedProvider()
        def stream():
            try:
                if continuation and sum(call.get('stream') is True for call in calls) > 1:
                    tail = json.dumps({'answer': '接着写完的第三段。', 'citations': [99] if invalid_citations else [1]}, ensure_ascii=False)
                    yield {'choices': [{'delta': {'content': tail}, 'finish_reason': None}]}
                    yield {'choices': [{'delta': {}, 'finish_reason': 'stop'}],
                           'usage': {'prompt_tokens': 7, 'completion_tokens': 3}}
                    return
                yield {'choices': [{'delta': {'content': raw}, 'finish_reason': None}],
                       'usage': {'prompt_tokens': 9, 'completion_tokens': 4}}
                if revoke:
                    from backend.memory_app.v2.privacy import set_private_project
                    set_private_project(state.records, 'alpha', True, 0)
                raise ConnectionError('synthetic disconnected provider')
            finally:
                closed.append(True)
        return stream()
    model = ModelConfiguration(state.records, state.root, InMemorySecretStore(), completion_fn=completion)
    model.update('generation', {'base_url': 'https://api.deepseek.com', 'model': 'deepseek-flash',
        'api_key': 'synthetic-private-value', 'allow_remote': True, 'expected_revision': 0})
    publish(state)
    app, domains = assemble(state.root, state.records, state.documents, state.service, model)
    return app, domains, model, calls, closed, complete


def test_body_disconnect_saves_complete_paragraphs_without_answer_usage_or_history(env):
    app, domains, model, calls, closed, complete = interrupted_app(env)
    before = {name: env.records.list(name) for name in ('v2_usage_insight', 'v2_usage_document')}
    with TestClient(app) as http:
        response = http.post('/api/v2/workbench/turns', json={'project_id': 'alpha', 'text': 'alpha?'},
            headers={'Accept': 'text/event-stream', 'Idempotency-Key': 'partial-one'})
        parts = events(response)
        assert parts[-1][0] == 'done', parts
        result = parts[-1][1]
        turn = result['turn']
        receipt = turn['receipt']['ask']
        assert receipt['answer'] is None and receipt['citations'] == []
        assert receipt['partial'] == complete
        assert receipt['interruption'] == 'connection'
        assert receipt['model_usage'] == {'input_tokens': 9, 'output_tokens': 4, 'total_tokens': 13}
        assert not receipt['no_match']
        assert app.state.ai_turn_store.get_immutable_payload(turn['id'], 'product-answer-result-v2') is None
        assert app.state.ai_runtime.receipt_for(turn['id']).status == 'waiting_approval'
        rows = app.state.ai_turn_store.events_after(turn['id'])
        dispatched = [row for row in rows if row['type'] == 'model.attempt.dispatched']
        terminals = [row for row in rows if row['type'] == 'model.attempt.terminal']
        assert len(dispatched) == len(terminals) == 1
        assert not any(row['type'] in {'approval.required', 'turn.completed', 'turn.failed'} for row in rows)
        assert app.state.ai_turn_store.get(terminals[0]['data']['receipt_ref'])['status'] == 'failed_transport'
        assert read_history(env.records, 'alpha', result['thread_id'], '接着问', query=domains.query)['turns'] == []
        assert {name: env.records.list(name) for name in before} == before
        saved = http.get(f"/api/v2/workbench/threads/{result['thread_id']}?project_id=alpha").json()
        assert saved['turns'] == [turn]
        replay = http.post('/api/v2/workbench/turns', json={'project_id': 'alpha', 'text': 'alpha?'},
            headers={'Idempotency-Key': 'partial-one'})
        assert replay.json() == result
    restarted, _ = assemble(env.root, env.records, env.documents, env.service, model)
    with TestClient(restarted) as http:
        assert http.get(f"/api/v2/workbench/threads/{result['thread_id']}?project_id=alpha").json()['turns'] == [turn]
    assert len(calls) == 1 and closed == [True]
    assert 'synthetic-private-value' not in response.text


def test_revoked_source_after_body_is_not_an_eligible_partial(env):
    app, _, _, calls, closed, _ = interrupted_app(env, revoke=True)
    with TestClient(app) as http:
        response = http.post('/api/v2/workbench/turns', json={'project_id': 'alpha', 'text': 'alpha?'},
            headers={'Accept': 'text/event-stream', 'Idempotency-Key': 'revoked-partial'})
        parts = events(response)
        assert parts[-1][0] == 'error'
        assert env.records.list('v2_turns') == ()
        assert env.records.list('v2_turn_frames') == ()
    assert len(calls) == 1 and closed == [True]


def test_provider_close_failure_never_grants_partial_continuation(env):
    app, _, _, calls, closed, _ = interrupted_app(env, close_failure=True)
    with TestClient(app) as http:
        response = http.post('/api/v2/workbench/turns', json={'project_id': 'alpha', 'text': 'alpha?'},
            headers={'Accept': 'text/event-stream', 'Idempotency-Key': 'unclosed-partial'})
        assert events(response)[-1] == ('error', {'code': 'answer_generation_failed'})
        assert env.records.list('v2_turns') == ()
        assert env.records.list('v2_turn_frames') == ()
        assert env.records.list('v2_answer_continuations') == ()
    assert len(calls) == 1 and closed == [False]


def _crash_driver(root):
    from threading import Event
    from backend.recognition import RecognitionService
    from core.document_engine import SQLiteDocumentRepository
    from core.storage_provider import SQLiteStructuredRecordStore
    root = Path(root)
    records = SQLiteStructuredRecordStore(root / 'records.sqlite3')
    state = SimpleNamespace(root=root, records=records, documents=SQLiteDocumentRepository(records),
                            service=RecognitionService(records))
    with records.begin() as tx:
        tx.put('v2_threads', 'thread-crash', {'project_id': 'alpha', 'title': 'alpha?',
            'created_at': '2026-10-05T00:00:00Z', 'updated_at': '2026-10-05T00:00:00Z'}, expected_revision=0)
        tx.commit()
    def provider(**request):
        (root / 'wire-count.txt').write_text('1')
        def stream():
            text = '完整段落 [1] ' + '正文' * 230 + '。\n\n未完成'
            yield {'choices': [{'delta': {'content': '{"answer":' + json.dumps(text, ensure_ascii=False)[:-1]},
                                'finish_reason': None}]}
            frames = records.list('v2_turn_frames')
            (root / 'ready.json').write_text(json.dumps({'turn_id': frames[0].object_id if frames else None}))
            Event().wait()
        return stream()
    model = ModelConfiguration(records, root, InMemorySecretStore(), completion_fn=provider)
    model.update('generation', {'base_url': 'https://api.deepseek.com', 'model': 'deepseek-flash',
        'api_key': 'synthetic-private-value', 'allow_remote': True, 'expected_revision': 0})
    app, _ = assemble(root, records, state.documents, state.service, model)
    publish(state)
    with TestClient(app) as http:
        http.post('/api/v2/workbench/turns', json={'project_id': 'alpha', 'thread_id': 'thread-crash', 'text': 'alpha?'},
                  headers={'Accept': 'text/event-stream', 'Idempotency-Key': 'crash-one'})


def test_process_killed_during_generation_reopens_frames_without_model_dispatch(tmp_path):
    from backend.recognition import RecognitionService
    from core.document_engine import SQLiteDocumentRepository
    from core.storage_provider import SQLiteStructuredRecordStore
    root = tmp_path / 'crash-root'
    root.mkdir()
    child_env = {**os.environ, 'CHRIPTMAS_APP_ROOT': str(root),
        'PYTHONPATH': str(Path(__file__).parents[3] / 'src') + os.pathsep + os.environ.get('PYTHONPATH', '')}
    with (root / 'child-output.log').open('w') as output:
        child = subprocess.Popen([sys.executable, '-c',
            'from tests.memory_app.v2.test_partial_answer import _crash_driver; import sys; _crash_driver(sys.argv[1])',
            str(root)], env=child_env, stdout=output, stderr=subprocess.STDOUT)
        try:
            deadline = time.monotonic() + 45
            while not (root / 'ready.json').is_file() and time.monotonic() < deadline and child.poll() is None:
                time.sleep(.03)
            assert (root / 'ready.json').is_file(), (root / 'child-output.log').read_text()
            identity = json.loads((root / 'ready.json').read_text())['turn_id']
            assert identity is not None
            child.kill()
            child.wait(timeout=10)
        finally:
            if child.poll() is None:
                child.kill()
                child.wait(timeout=10)
    assert (root / 'wire-count.txt').read_text() == '1'
    records = SQLiteStructuredRecordStore(root / 'records.sqlite3')
    documents, service = SQLiteDocumentRepository(records), RecognitionService(records)
    new_calls = []
    def unexpected(**request):
        new_calls.append(request)
        raise AssertionError('read-only recovery must not dispatch')
    model = ModelConfiguration(records, root, InMemorySecretStore(), completion_fn=unexpected)
    app, _ = assemble(root, records, documents, service, model)
    with TestClient(app) as http:
        response = http.get('/api/v2/workbench/threads/thread-crash?project_id=alpha')
        assert response.status_code == 200
        turns = response.json()['turns']
        assert len(turns) == 1
        assert turns[0]['id'] == identity
        assert turns[0]['receipt']['ask']['answer'] is None
        assert turns[0]['receipt']['ask']['partial'].endswith('。\n\n')
        assert turns[0]['receipt']['ask']['interruption'] == 'connection'
        assert http.get('/api/v2/workbench/threads/thread-crash?project_id=other').status_code == 404
    assert new_calls == [] and (root / 'wire-count.txt').read_text() == '1'


def test_explicit_continue_adds_an_attempt_to_same_turn_and_validates_complete_citations(env):
    app, domains, _, calls, closed, complete = interrupted_app(env, continuation=True)
    with TestClient(app) as http:
        first = http.post('/api/v2/workbench/turns', json={'project_id': 'alpha', 'text': 'alpha?'},
            headers={'Accept': 'text/event-stream', 'Idempotency-Key': 'continue-source'})
        saved = events(first)[-1][1]
        identity = saved['turn']['id']
        result = http.post(f'/api/v2/workbench/turns/{identity}/continue', json={'project_id': 'alpha'},
            headers={'Idempotency-Key': 'continue-one'})
        assert result.status_code == 200, result.text
        final = result.json()
        assert final['id'] == identity
        receipt = final['receipt']['ask']
        assert receipt['answer'] == complete + '接着写完的第三段。'
        assert len(receipt['citations']) == 1 and receipt['citations'][0]['n'] == 1
        assert 'partial' not in receipt and 'interruption' not in receipt
        assert receipt['model_usage'] == {'input_tokens': 16, 'output_tokens': 7, 'total_tokens': 23}
        rows = app.state.ai_turn_store.events_after(identity)
        attempts = [row for row in rows if row['type'] == 'model.attempt.dispatched']
        assert len(attempts) == 2 and {row['turn_id'] for row in attempts} == {identity}
        assert len({row['correlation']['model_request_id'] for row in attempts}) == 2
        assert app.state.ai_turn_store.get_immutable_payload(identity, 'product-answer-result-v2')[1]['receipt']['ask']['answer'] == receipt['answer']
        assert len(read_history(env.records, 'alpha', saved['thread_id'], '下一问', query=domains.query)['turns']) == 1
        repeat = http.post(f'/api/v2/workbench/turns/{identity}/continue', json={'project_id': 'alpha'},
            headers={'Idempotency-Key': 'continue-one'})
        assert repeat.json() == final
    assert len(calls) == 2 and closed == [True, True]
    assert {'role': 'assistant', 'content': complete} in calls[1]['messages']


@pytest.mark.parametrize('change', ['missing_request', 'question', 'intent', 'sequence', 'duplicate', 'text_type', 'retry_version'])
def test_thread_read_rejects_unbound_or_forged_orphan_frames_without_dispatch(env, change):
    from core.ai_kernel.sqlite_store import SQLiteAITurnStore
    from tests.rebuild.test_product_turn_kinds import request
    identity = 'turn-' + 'b' * 32
    frozen = request('project.answer', turn_id=identity, project_id='alpha', text='alpha?',
        template_version=2, capabilities=['workbench.answer.execute'])
    if change == 'retry_version':
        frozen['policy_versions'] = {'retry': '@999999'}
    if change != 'missing_request':
        SQLiteAITurnStore(env.root / '.rebuild-data/ai-turns.sqlite3').claim_turn(frozen)
    turn = {'id': identity, 'thread_id': 'thread-framed', 'intent': 'ask', 'user_text': 'alpha?',
        'created_at': '2026-10-05T00:00:00Z'}
    values = [{'sequence': 1, 'text': '完整段落。\n\n未完成'}]
    if change == 'question':
        turn['user_text'] = 'forged question'
    elif change == 'intent':
        turn['intent'] = 'do'
    elif change == 'sequence':
        values[0]['sequence'] = 4
    elif change == 'duplicate':
        values += values
    elif change == 'text_type':
        values[0]['text'] = {'forged': 'text'}
    with env.records.begin() as tx:
        tx.put('v2_threads', 'thread-framed', {'project_id': 'alpha', 'title': 'alpha?',
            'created_at': turn['created_at'], 'updated_at': turn['created_at']}, expected_revision=0)
        tx.put('v2_turn_frames', identity, {'project_id': 'alpha', 'thread_id': 'thread-framed',
            'turn': turn, 'frames': values}, expected_revision=0)
        tx.commit()
    response = env.http.get('/api/v2/workbench/threads/thread-framed?project_id=alpha')
    assert response.status_code == 200
    assert response.json()['turns'] == []
    assert env.model.calls == 0


@pytest.mark.parametrize('frame_failure', [False, True])
def test_completed_generation_removes_frames_and_ignores_real_frame_write_failure(env, frame_failure):
    calls, closed = [], []
    answer = '完整正文' * 110 + ' [1]。'
    def provider(**request):
        calls.append(request)
        def stream():
            try:
                raw = json.dumps({'answer': answer, 'citations': [1]}, ensure_ascii=False)
                yield {'choices': [{'delta': {'content': raw}, 'finish_reason': None}]}
                yield {'choices': [{'delta': {}, 'finish_reason': 'stop'}],
                    'usage': {'prompt_tokens': 3, 'completion_tokens': 2}}
            finally:
                closed.append(True)
        return stream()
    model = ModelConfiguration(env.records, env.root, InMemorySecretStore(), completion_fn=provider)
    model.update('generation', {'base_url': 'https://api.deepseek.com', 'model': 'deepseek-flash',
        'api_key': 'synthetic-private-value', 'allow_remote': True, 'expected_revision': 0})
    publish(env)
    with sqlite3.connect(env.records.database_path) as connection:
        connection.execute('CREATE TABLE frame_writes (sequence INTEGER)')
        if frame_failure:
            connection.execute("CREATE TRIGGER reject_frame BEFORE INSERT ON crp_structured_records "
                "WHEN NEW.collection='v2_turn_frames' BEGIN SELECT RAISE(ABORT,'synthetic frame failure'); END")
        else:
            connection.execute("CREATE TRIGGER record_frame AFTER INSERT ON crp_structured_records "
                "WHEN NEW.collection='v2_turn_frames' BEGIN INSERT INTO frame_writes VALUES(1); END")
    app, _ = assemble(env.root, env.records, env.documents, env.service, model)
    with TestClient(app) as http:
        response = http.post('/api/v2/workbench/turns', json={'project_id': 'alpha', 'text': 'alpha?'},
            headers={'Accept': 'text/event-stream', 'Idempotency-Key': 'frame-completion'})
        parts = events(response)
        assert parts[-1][0] == 'done', parts
        result = parts[-1][1]['turn']
        assert result['receipt']['ask']['answer'] == answer
        assert len(result['receipt']['ask']['citations']) == 1
        assert app.state.ai_runtime.receipt_for(result['id']).status == 'completed'
        assert env.records.list('v2_turn_frames') == ()
    with sqlite3.connect(env.records.database_path) as connection:
        assert connection.execute('SELECT COUNT(*) FROM frame_writes').fetchone()[0] == (0 if frame_failure else 1)
    assert len(calls) == 1 and closed == [True]
