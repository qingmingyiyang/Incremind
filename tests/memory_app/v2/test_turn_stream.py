"""Read-only stream replay through real product/SQLite/model attempt owners."""
import json
import asyncio
import sqlite3
from copy import deepcopy
from contextlib import closing
from concurrent.futures import ThreadPoolExecutor
from threading import Event
from time import monotonic, sleep
from urllib.error import HTTPError

import pytest
from fastapi.testclient import TestClient
from starlette.requests import Request

from tests.memory_app.v2.test_workbench_ask import env, assemble
from tests.memory_app.v2.test_workbench_stream import native_app, events
from tests.memory_app.v2.test_partial_answer import interrupted_app
from tests.memory_app.v2.test_workbench_do import env as do_env


def stream_events(response):
    values = []
    for block in response.text.split('\n\n'):
        lines = block.splitlines()
        data = [line.removeprefix('data: ') for line in lines if line.startswith('data: ')]
        if not data:
            continue
        ids = [line.removeprefix('id: ') for line in lines if line.startswith('id: ')]
        assert len(ids) == 1 and ids[0].isascii() and ids[0].isdigit(), block
        kind = next(line.removeprefix('event: ') for line in lines if line.startswith('event: '))
        values.append((int(ids[0]), kind, json.loads('\n'.join(data))))
    return values


def stream_endpoint(app):
    def routes(router):
        for route in router.routes:
            yield route
            if hasattr(route, 'original_router'):
                yield from routes(route.original_router)
    return next(route.endpoint for route in routes(app)
        if getattr(route, 'path', '') == '/api/v2/workbench/turns/{turn_id}/stream')


def ready_stream_events(app, identity, project, count):
    async def read():
        response = await stream_endpoint(app)(identity, Request({'type': 'http', 'headers': []}),
            project_id=project, after='0')
        iterator, chunks = response.body_iterator, []
        try:
            for _ in range(count):
                chunks.append(await anext(iterator))
        finally:
            await iterator.aclose()
        return ''.join(chunks)
    raw = asyncio.run(asyncio.wait_for(read(), 1))
    class ObservedResponse:
        text = raw
    return stream_events(ObservedResponse())


@pytest.mark.parametrize('derived_failure', [False, True])
def test_primary_ask_retry_status_is_safe_readonly_and_best_effort(env, derived_failure, caplog):
    from time import time
    app, model, calls, closed = native_app(env)
    original = model._completion_fn
    arrived, release, wires = Event(), Event(), []

    def external_provider(**request):
        wires.append(request)
        if len(wires) == 1:
            raise HTTPError('https://synthetic.invalid', 503, 'synthetic overload', {'Retry-After': '1'}, None)
        arrived.set()
        assert release.wait(25), 'external provider body barrier expired'
        return original(**request)

    model._completion_fn = external_provider
    if derived_failure:
        with env.records.begin() as tx:
            tx.connection.execute("""CREATE TRIGGER reject_retry_display BEFORE UPDATE ON crp_structured_records
                WHEN NEW.collection='v2_turn_streams' AND EXISTS (
                    SELECT 1 FROM json_each(NEW.payload_json,'$.events')
                    WHERE json_extract(value,'$.state')='retrying')
                BEGIN SELECT RAISE(ABORT,'derived_retry_unavailable'); END""")
            tx.commit()
    with TestClient(app) as http, ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(http.post, '/api/v2/workbench/turns',
            json={'project_id': 'alpha', 'text': 'alpha?'},
            headers={'Accept': 'text/event-stream', 'Idempotency-Key': 'retry-display'})
        try:
            assert arrived.wait(25), 'provider retry did not begin'
            heads = env.records.list('v2_turn_streams')
            assert len(heads) == 1
            head = heads[0]
            identity = head.object_id
            retry_events = [e for e in head.payload['events'] if e.get('state') == 'retrying']
            assert len(retry_events) == (0 if derived_failure else 1)
            kernel = app.state.ai_turn_store.events_after(identity)
            terminals = [app.state.ai_turn_store.get(e['data']['receipt_ref']) for e in kernel
                if e['type'] == 'model.attempt.terminal']
            assert [r['status'] for r in terminals] == ['failed_transport']
            assert len(wires) == 2 and calls == [] and closed == []
            before = {name: env.records.list(name) for name in ('v2_turn_streams', 'v2_turn_frames', 'v2_turns')}
            replay = ready_stream_events(app, identity, 'alpha', len(head.payload['events']))
            assert env.records.list('v2_turn_streams') == before['v2_turn_streams']
            assert env.records.list('v2_turn_frames') == before['v2_turn_frames']
            assert env.records.list('v2_turns') == before['v2_turns']
            assert app.state.ai_turn_store.events_after(identity) == kernel and len(wires) == 2
            if not derived_failure:
                status = next(value for _, event, value in replay if event == 'status')
                assert set(status) == {'state', 'retry', 'server_time'} and status['state'] == 'retrying'
                observed = status['retry']
                assert set(observed) == {'attempt', 'delay', 'budget', 'reason', 'used', 'limit', 'retry_at'}
                assert {k: observed[k] for k in observed if k != 'retry_at'} == {
                    'attempt': 1, 'delay': 1.0, 'budget': 'before_output', 'reason': 'server', 'used': 1, 'limit': 10}
                assert abs(status['server_time'] - time()) < 2
                assert 0 <= status['server_time'] - observed['retry_at'] < 2
                assert 'test-private-value' not in json.dumps(status)
                changed = deepcopy(head.payload)
                next(e for e in changed['events'] if e.get('state') == 'retrying')['retry']['used'] = True
                with env.records.begin() as tx:
                    tx.put('v2_turn_streams', identity, changed, expected_revision=head.revision)
                    tx.commit()
                corrupt = env.records.read('v2_turn_streams', identity)
                refused = http.get(f'/api/v2/workbench/turns/{identity}/stream?project_id=alpha&after=0')
                assert refused.status_code == 404
                assert http.get(f'/api/v2/workbench/turns/{identity}/stream?project_id=alpha&after={head.payload["sequence"]}').status_code == 404
                assert env.records.read('v2_turn_streams', identity) == corrupt
                assert app.state.ai_turn_store.events_after(identity) == kernel and len(wires) == 2
                with env.records.begin() as tx:
                    tx.put('v2_turn_streams', identity, head.payload, expected_revision=corrupt.revision)
                    tx.commit()
        finally:
            release.set()
        response = future.result(timeout=25)
        assert response.status_code == 200, response.text
        parts = stream_events(response)
        final = parts[-1][2]
        assert final['turn']['receipt']['ask']['answer'] == '流式答案😀\n第二行'
        live_retry = [value for _, event, value in parts if event == 'status' and value['state'] == 'retrying']
        assert len(live_retry) == (0 if derived_failure else 1)
        head = env.records.read('v2_turn_streams', identity)
        assert head.payload['events'][-2]['state'] == 'completed'
        assert env.records.read('v2_turn_frames', identity) is None
        assert len(wires) == 2 and len(calls) == 1 and closed == [True]
        if derived_failure:
            assert sum(record.message == 'turn_retry_projection_failed' for record in caplog.records) == 1
        completed = http.get(f'/api/v2/workbench/turns/{identity}/stream?project_id=alpha&after=0')
        assert stream_events(completed)[-1][1:] == ('done', final)


def test_main_retry_status_uses_canonical_turn_and_worker_retries_stay_private(do_env):
    http, model = do_env
    records = http.app.state.recognition_service.records
    configure_do_provider(model, '真实主智能体完整汇总。')
    original = model._completion_fn
    arrived, release, wire_roles, worker_heads = Event(), Event(), [], []
    counts = {'main': 0, 'worker': 0}

    def external_provider(**request):
        context = json.loads(request['messages'][-1]['content'])
        role = ('steward' if 'output' in context else 'main' if any(
            c['capability_id'] == 'agent.list' for c in context.get('capabilities', [])) else 'worker')
        wire_roles.append(role)
        if role in counts:
            counts[role] += 1
            if counts[role] == 1:
                if role == 'worker':
                    worker_heads.append(records.list('v2_turn_streams'))
                raise HTTPError('https://synthetic.invalid', 503, 'synthetic overload', {'Retry-After': '1'}, None)
        if role == 'main':
            arrived.set()
            assert release.wait(25), 'Main provider body barrier expired'
        return original(**request)

    model._completion_fn = external_provider
    with ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(http.post, '/api/v2/workbench/turns',
            json={'project_id': 'alpha', 'intent': 'do', 'text': '请准备一份完整草稿'},
            headers={'Accept': 'text/event-stream', 'Idempotency-Key': 'main-retry-display'})
        try:
            assert arrived.wait(25), 'Main provider retry did not begin'
            heads = records.list('v2_turn_streams')
            assert len(heads) == 1
            head = heads[0]
            statuses = [e for e in head.payload['events'] if e.get('state') == 'retrying']
            assert len(statuses) == 1
            assert worker_heads == [()]
            assert wire_roles.count('worker') == 2 and wire_roles.count('main') == 2
            request = head.payload['binding']['request']
            assert request == http.app.state.ai_turn_store.get_request(request['turn_id'])
            assert request['scope']['project_id'] == 'alpha'
            product = records.read('v2_turns', head.object_id)
            assert product.payload['receipt']['do']['kernel_turn_id'] == request['turn_id']
            kernel = http.app.state.ai_turn_store.events_after(request['turn_id'])
            terminals = [http.app.state.ai_turn_store.get(e['data']['receipt_ref']) for e in kernel
                if e['type'] == 'model.attempt.terminal']
            assert [r['status'] for r in terminals] == ['failed_transport']
            before = {name: records.list(name) for name in ('v2_turn_streams', 'v2_turn_frames', 'v2_turns')}
            replay = ready_stream_events(http.app, head.object_id, 'alpha', len(head.payload['events']))
            status = next(value for _, event, value in replay if event == 'status')
            assert status['retry']['budget'] == 'before_output' and status['retry']['limit'] == 10
            assert status['retry']['reason'] == 'server' and status['retry']['used'] == 1
            assert {name: records.list(name) for name in before} == before
            assert http.app.state.ai_turn_store.events_after(request['turn_id']) == kernel
        finally:
            release.set()
        response = future.result(timeout=25)
    assert response.status_code == 200, response.text
    first, remaining = response.text.split('\n\n', 1)
    assert first.startswith('event: started\n') and '\nid: ' not in first
    started = json.loads(first.removeprefix('event: started\ndata: '))
    assert started['turn']['id'] == head.object_id and started['thread_id'] == head.payload['thread_id']
    class PersistedResponse:
        text = remaining
    parts = stream_events(PersistedResponse())
    assert len([value for _, event, value in parts if event == 'status' and value['state'] == 'retrying']) == 1
    result = parts[-1][2]
    assert result['turn']['receipt']['do']['state'] == 'done'
    assert result['turn']['receipt']['do']['document_id']
    assert len(wire_roles) == 5 and wire_roles.count('worker') == 2 and wire_roles.count('main') == 2
    assert records.read('v2_turn_frames', result['turn']['id']) is None
    rows = records.list('v2_task_draft_operations')
    assert len(rows) == 2


@pytest.mark.parametrize('revoke', [False, True])
def test_ask_retry_attempts_are_ordered_and_current_privacy_blocks_new_wire(env, revoke):
    from backend.memory_app.v2.privacy import set_private_project
    from backend.memory_app.v2.followup import read_history
    app, model, calls, closed = native_app(env)
    original, wires = model._completion_fn, []
    usage_before = {name: env.records.list(name) for name in ('v2_usage_insight', 'v2_usage_document')}

    def external_provider(**request):
        wires.append(request)
        if len(wires) <= 2:
            if revoke:
                set_private_project(env.records, 'alpha', True)
            raise HTTPError('https://synthetic.invalid', 503, 'synthetic overload', {'Retry-After': '1'}, None)
        return original(**request)

    model._completion_fn = external_provider
    with TestClient(app) as http:
        response = http.post('/api/v2/workbench/turns', json={'project_id': 'alpha', 'text': 'alpha?'},
            headers={'Accept': 'text/event-stream', 'Idempotency-Key': 'ordered-retry-display'})
        assert response.status_code == 200
        parts = events(response)
        identity = parts[0][1]['turn']['id']
        head = env.records.read('v2_turn_streams', identity)
        retry_events = [e for e in head.payload['events'] if e.get('state') == 'retrying']
        kernel = app.state.ai_turn_store.events_after(identity)
        terminals = [app.state.ai_turn_store.get(e['data']['receipt_ref']) for e in kernel
            if e['type'] == 'model.attempt.terminal']
        if revoke:
            assert len(wires) == 1 and calls == [] and closed == []
            assert retry_events == [] and not any(name == 'status' for name, _ in parts)
            assert parts[-1] == ('error', {'code': 'answer_generation_failed'})
            assert kernel[-1]['type'] == 'turn.failed'
            assert [r['status'] for r in terminals] == ['failed_transport']
            assert env.records.list('v2_turns') == ()
            assert env.records.read('v2_turn_requests', 'ordered-retry-display').payload['state'] == 'failed'
            history = read_history(env.records, 'alpha', parts[0][1]['thread_id'], 'next question')
            assert history['turns'] == [] and history['text'] == ''
            assert {name: env.records.list(name) for name in usage_before} == usage_before
        else:
            assert len(wires) == 3 and len(calls) == 1 and closed == [True]
            assert [(e['retry']['attempt'], e['retry']['used'], e['retry']['limit']) for e in retry_events] == [
                (1, 1, 10), (2, 2, 10)]
            assert [r['status'] for r in terminals] == ['failed_transport', 'failed_transport', 'succeeded']
            persistent = stream_events(response)
            sequences = [seq for seq, _, _ in persistent]
            assert sequences == sorted(set(sequences))
            assert len([v for _, name, v in persistent if name == 'status' and v['state'] == 'retrying']) == 2
            assert parts[-1][0] == 'done' and parts[-1][1]['turn']['receipt']['ask']['answer'] == '流式答案😀\n第二行'
            assert head.payload['events'][-2]['state'] == 'completed'
            assert env.records.read('v2_turn_frames', identity) is None


def test_completed_stream_replays_done_after_frames_deleted_without_dispatch(env):
    app, _, calls, closed = native_app(env)
    with TestClient(app) as http:
        original = http.post('/api/v2/workbench/turns', json={'project_id': 'alpha', 'text': 'alpha?'},
            headers={'Accept': 'text/event-stream', 'Idempotency-Key': 'completed-stream'})
        assert original.status_code == 200
        final = events(original)[-1][1]
        identity = final['turn']['id']
        before = app.state.ai_turn_store.events_after(identity)
        assert env.records.read('v2_turn_frames', identity) is None
        result = http.get(f'/api/v2/workbench/turns/{identity}/stream?project_id=alpha&after=0')
        assert result.status_code == 200, result.text
        replay = stream_events(result)
        assert len(replay) == 1 and replay[0][1:] == ('done', final)
        assert replay[0][0] > 0
        assert result.headers['X-Accel-Buffering'] == 'no'
        assert result.headers['Cache-Control'] == 'no-store'
        assert app.state.ai_turn_store.events_after(identity) == before
    assert len(calls) == 1 and closed == [True]


def test_interrupted_frames_replay_reset_and_cursor_on_new_app_without_authority(env):
    app, _, model, calls, closed, complete = interrupted_app(env)
    with TestClient(app) as http:
        original = http.post('/api/v2/workbench/turns', json={'project_id': 'alpha', 'text': 'alpha?'},
            headers={'Accept': 'text/event-stream', 'Idempotency-Key': 'interrupted-stream'})
        assert original.status_code == 200
        final = events(original)[-1][1]
        identity = final['turn']['id']
        before = app.state.ai_turn_store.events_after(identity)
        rows_before = {name: env.records.list(name) for name in
            ('v2_turns', 'v2_turn_requests', 'v2_turn_frames', 'v2_turn_streams', 'v2_task_continuations')}
    restarted, _ = assemble(env.root, env.records, env.documents, env.service, model)
    with TestClient(restarted) as http:
        response = http.get(f'/api/v2/workbench/turns/{identity}/stream?project_id=alpha&after=0')
        assert response.status_code == 200, response.text
        replay = stream_events(response)
        assert all(next_id == prior_id + 1 for (prior_id, *_), (next_id, *_) in zip(replay, replay[1:]))
        displayed = ''
        for _, kind, payload in replay:
            if kind == 'delta':
                displayed += payload['text']
            elif kind == 'reset':
                displayed = payload['text']
        assert displayed == complete
        assert any(kind == 'status' and payload['state'] == 'interrupted' for _, kind, payload in replay)
        assert replay[-1][1:] == ('done', final)
        delta_id = next(sequence for sequence, kind, _ in replay if kind == 'delta')
        suffix = http.get(f'/api/v2/workbench/turns/{identity}/stream?project_id=alpha',
            headers={'Last-Event-ID': str(delta_id)})
        assert suffix.status_code == 200
        assert stream_events(suffix) == [item for item in replay if item[0] > delta_id]
        assert {name: env.records.list(name) for name in rows_before} == rows_before
    assert app.state.ai_turn_store.events_after(identity) == before
    assert len(calls) == 1 and closed == [True]


def test_completed_stream_rejects_scope_cursor_and_corrupt_projection_without_wire(env):
    app, _, calls, closed = native_app(env)
    with TestClient(app) as http:
        posted = http.post('/api/v2/workbench/turns', json={'project_id': 'alpha', 'text': 'alpha?'},
            headers={'Accept': 'text/event-stream', 'Idempotency-Key': 'stream-negative'})
        assert posted.status_code == 200
        final = events(posted)[-1][1]
        identity = final['turn']['id']
        path = f'/api/v2/workbench/turns/{identity}/stream'
        original = env.records.read('v2_turn_streams', identity)
        kernel = app.state.ai_turn_store.events_after(identity)
        before = {name: env.records.list(name) for name in
            ('v2_turns', 'v2_turn_requests', 'v2_turn_frames', 'v2_turn_streams')}
        assert http.get(path + '?project_id=beta').status_code == 404
        for cursor in ('-1', '01', '1.0', '9007199254740992', str(original.payload['sequence'] + 1)):
            assert http.get(path + '?project_id=alpha&after=' + cursor).status_code == 400
        assert http.get(path + '?project_id=alpha&after=0', headers={'Last-Event-ID': '1'}).status_code == 400
        assert {name: env.records.list(name) for name in before} == before
        corruptions = []
        changed = deepcopy(original.payload)
        changed['events'][0]['sequence'] = 999
        corruptions.append(changed)
        changed = deepcopy(original.payload)
        changed['binding']['request']['input']['text'] = 'a different request'
        corruptions.append(changed)
        changed = deepcopy(original.payload)
        changed['thread_id'] = 'thread-not-bound'
        corruptions.append(changed)
        for payload in corruptions:
            with env.records.begin() as tx:
                row = tx.read('v2_turn_streams', identity)
                tx.put('v2_turn_streams', identity, payload, expected_revision=row.revision)
                tx.commit()
            corrupted = env.records.read('v2_turn_streams', identity)
            response = http.get(path + '?project_id=alpha&after=0')
            assert response.status_code == 404, response.text
            assert env.records.read('v2_turn_streams', identity) == corrupted
        assert app.state.ai_turn_store.events_after(identity) == kernel
    assert len(calls) == 1 and closed == [True]


@pytest.mark.parametrize('phase', ['start', 'terminal'])
def test_derived_head_write_failure_keeps_real_answer_and_readonly_terminal_without_fake_id(env, phase):
    app, _, calls, closed = native_app(env)
    statement = ('INSERT' if phase == 'start' else 'UPDATE')
    condition = ('' if phase == 'start' else " AND json_extract(NEW.payload_json,'$.terminal') IS NOT NULL")
    with env.records.begin() as tx:
        tx.connection.execute(f"""CREATE TRIGGER reject_derived_stream BEFORE {statement}
            ON crp_structured_records WHEN NEW.collection='v2_turn_streams'{condition}
            BEGIN SELECT RAISE(ABORT,'derived_stream_unavailable'); END""")
        tx.commit()
    with TestClient(app) as http:
        posted = http.post('/api/v2/workbench/turns', json={'project_id': 'alpha', 'text': 'alpha?'},
            headers={'Accept': 'text/event-stream', 'Idempotency-Key': 'derived-write-failure'})
        assert posted.status_code == 200
        final = events(posted)[-1][1]
        assert final['turn']['receipt']['ask']['answer'] == '流式答案😀\n第二行'
        identity = final['turn']['id']
        kernel = app.state.ai_turn_store.events_after(identity)
        assert kernel[-1]['type'] == 'turn.completed'
        head = env.records.read('v2_turn_streams', identity)
        assert (head is None) if phase == 'start' else (head is not None and head.payload['terminal'] is None)
        before = {name: env.records.list(name) for name in
            ('v2_turns', 'v2_turn_requests', 'v2_turn_frames', 'v2_turn_streams')}
        endpoint = stream_endpoint(app)
        async def read_ready_terminal():
            response = await endpoint(identity, Request({'type': 'http', 'headers': []}), project_id='alpha', after='0')
            chunks = []
            async for chunk in response.body_iterator:
                chunks.append(chunk)
            return ''.join(chunks)
        # A persisted completed answer is immediately readable. This bound only
        # prevents a derived-write regression from leaving this test polling.
        replay = asyncio.run(asyncio.wait_for(read_ready_terminal(), .25))
        assert replay == 'event: done\ndata: ' + json.dumps(final, ensure_ascii=False) + '\n\n'
        assert {name: env.records.list(name) for name in before} == before
        assert app.state.ai_turn_store.events_after(identity) == kernel
    assert len(calls) == 1 and closed == [True]


def test_failed_ask_stream_finishes_with_safe_error_without_result_or_dispatch(env):
    app, _, calls, closed = native_app(env, invalid=True)
    with TestClient(app) as http:
        posted = http.post('/api/v2/workbench/turns', json={'project_id': 'alpha', 'text': 'alpha?'},
            headers={'Accept': 'text/event-stream', 'Idempotency-Key': 'failed-stream-boundary'})
        parts = events(posted)
        assert parts[-1] == ('error', {'code': 'answer_generation_failed'})
        identity = parts[0][1]['turn']['id']
        assert env.records.read('v2_turns', identity) is None
        assert env.records.read('v2_turn_requests', 'failed-stream-boundary').payload['state'] == 'failed'
        kernel = app.state.ai_turn_store.events_after(identity)
        assert kernel[-1]['type'] == 'turn.failed' and kernel[-1]['data']['status'] == 'failed'
        before = {name: env.records.list(name) for name in
            ('v2_turns', 'v2_turn_requests', 'v2_turn_frames', 'v2_turn_streams', 'v2_answer_continuations')}
        database = env.root / '.rebuild-data/ai-turns.sqlite3'
        with closing(sqlite3.connect(database.as_uri() + '?mode=ro', uri=True)) as connection:
            immutable = connection.execute('SELECT kind,payload_ref FROM ai_turn_immutable_payloads WHERE turn_id=? ORDER BY kind',
                (identity,)).fetchall()
        async def read_finished_error():
            response = await stream_endpoint(app)(identity, Request({'type': 'http', 'headers': []}),
                project_id='alpha', after='0')
            return ''.join([chunk async for chunk in response.body_iterator])
        replay = asyncio.run(asyncio.wait_for(read_finished_error(), .25))
        assert replay == 'event: error\ndata: {"code": "answer_generation_failed"}\n\n'
        assert {name: env.records.list(name) for name in before} == before
        assert app.state.ai_turn_store.events_after(identity) == kernel
        with closing(sqlite3.connect(database.as_uri() + '?mode=ro', uri=True)) as connection:
            assert connection.execute('SELECT kind,payload_ref FROM ai_turn_immutable_payloads WHERE turn_id=? ORDER BY kind',
                (identity,)).fetchall() == immutable
    assert len(calls) == 1 and closed == [True]


def test_stream_get_never_creates_or_initializes_missing_or_incomplete_databases(env):
    app, _, calls, closed = native_app(env)
    with TestClient(app) as http:
        posted = http.post('/api/v2/workbench/turns', json={'project_id': 'alpha', 'text': 'alpha?'},
            headers={'Accept': 'text/event-stream', 'Idempotency-Key': 'stream-database-boundary'})
        assert posted.status_code == 200
        identity = events(posted)[-1][1]['turn']['id']
        kernel = app.state.ai_turn_store.events_after(identity)
        for path in (env.records.database_path, env.root / '.rebuild-data/ai-turns.sqlite3'):
            path.resolve().relative_to(env.root.resolve())
            for incomplete in (False, True):
                preserved = []
                for suffix in ('', '-wal', '-shm'):
                    source = path.with_name(path.name + suffix)
                    if source.exists():
                        target = source.with_name(source.name + '.test-preserved')
                        assert not target.exists()
                        source.rename(target)
                        preserved.append((source, target))
                try:
                    if incomplete:
                        with closing(sqlite3.connect(path)) as connection:
                            connection.execute('CREATE TABLE unrelated_fixture(value INTEGER)')
                            connection.commit()
                        with closing(sqlite3.connect(path.as_uri() + '?mode=ro', uri=True)) as connection:
                            before = tuple(connection.execute('SELECT name,sql FROM sqlite_master ORDER BY name'))
                    response = http.get(f'/api/v2/workbench/turns/{identity}/stream?project_id=alpha&after=0')
                    assert response.status_code == 404
                    if incomplete:
                        with closing(sqlite3.connect(path.as_uri() + '?mode=ro', uri=True)) as connection:
                            assert tuple(connection.execute('SELECT name,sql FROM sqlite_master ORDER BY name')) == before
                    else:
                        assert not path.exists(), 'GET created a missing database'
                finally:
                    # Only these validated synthetic fixture files are removed;
                    # preserve and restore the real fixture authority unchanged.
                    for suffix in ('', '-wal', '-shm'):
                        generated = path.with_name(path.name + suffix)
                        generated.resolve().relative_to(env.root.resolve())
                        if generated.exists():
                            generated.unlink()
                    for source, target in preserved:
                        target.rename(source)
        assert app.state.ai_turn_store.events_after(identity) == kernel
    assert len(calls) == 1 and closed == [True]


def test_stream_snapshot_and_cost_projection_use_only_real_readonly_sql_connections(env, monkeypatch):
    app, _, calls, closed = native_app(env)
    with TestClient(app) as http:
        posted = http.post('/api/v2/workbench/turns', json={'project_id': 'alpha', 'text': 'alpha?'},
            headers={'Accept': 'text/event-stream', 'Idempotency-Key': 'stream-readonly-sql'})
        assert posted.status_code == 200
        final = events(posted)[-1][1]
        identity = final['turn']['id']
        kernel = app.state.ai_turn_store.events_after(identity)
        opened, verbs, query_only = [], [], []
        original_connect = sqlite3.connect

        class TracedConnection(sqlite3.Connection):
            def set_trace_callback(self, callback):
                def observe(statement):
                    # Discard all content; retain only the SQL verb and one
                    # known static read-only pragma, never values or requests.
                    verbs.append(statement.lstrip().split(None, 1)[0].upper())
                    query_only.append(statement.replace(' ', '').upper() == 'PRAGMAQUERY_ONLY=ON')
                    if callback is not None:
                        callback(statement)
                return super().set_trace_callback(observe)

        def traced_connect(database, *args, **kwargs):
            opened.append((isinstance(database, str) and database.endswith('?mode=ro'), kwargs.get('uri') is True))
            assert 'factory' not in kwargs
            return original_connect(database, *args, **kwargs, factory=TracedConnection)

        with monkeypatch.context() as tracing:
            tracing.setattr(sqlite3, 'connect', traced_connect)
            replay = http.get(f'/api/v2/workbench/turns/{identity}/stream?project_id=alpha&after=0')
        assert replay.status_code == 200, replay.text
        assert stream_events(replay)[-1][1:] == ('done', final)
        assert len(opened) >= 3 and all(readonly and uri for readonly, uri in opened)
        assert verbs and set(verbs) <= {'SELECT', 'PRAGMA', 'BEGIN'}
        assert sum(query_only) >= 2
        assert app.state.ai_turn_store.events_after(identity) == kernel
    assert len(calls) == 1 and closed == [True]


def test_explicit_ask_continue_opens_committed_segment_and_get_replays_without_dispatch(env):
    app, _, model, calls, closed, complete = interrupted_app(env, continuation=True)
    provider = model._completion_fn
    arrived, release = Event(), Event()
    captured = {}
    tail = '接着写完的第三段。' * 60
    identity = None

    def external_provider(**request):
        response = provider(**request)
        if len(calls) == 1:
            return response
        captured['before_body'] = env.records.read('v2_turn_streams', identity)
        def stream():
            try:
                for index, chunk in enumerate(response):
                    if index == 0:
                        chunk = {'choices': [{'delta': {'content': json.dumps(
                            {'answer': tail, 'citations': [1]}, ensure_ascii=False)}, 'finish_reason': None}]}
                    yield chunk
                    if index == 0:
                        arrived.set()
                        assert release.wait(10), 'synthetic provider barrier was not released'
            finally:
                response.close()
        return stream()

    model._completion_fn = external_provider
    with TestClient(app) as http:
        initial = http.post('/api/v2/workbench/turns', json={'project_id': 'alpha', 'text': 'alpha?'},
            headers={'Accept': 'text/event-stream', 'Idempotency-Key': 'continued-stream-original'})
        original = events(initial)[-1][1]
        identity = original['turn']['id']
        old_head = env.records.read('v2_turn_streams', identity)
        old_cursor = old_head.payload['terminal']
        assert old_cursor == old_head.payload['sequence']
        assert original['turn']['receipt']['ask']['partial'] == complete
        kernel_before = app.state.ai_turn_store.events_after(identity)
        intents = [row for row in kernel_before if row['type'] == 'tool.intent.recorded']
        frozen = app.state.ai_turn_store.get_request(identity)
        with ThreadPoolExecutor(max_workers=1) as executor:
            continued = executor.submit(http.post, f'/api/v2/workbench/turns/{identity}/continue',
                json={'project_id': 'alpha'}, headers={'Idempotency-Key': 'continued-stream-explicit'})
            try:
                assert arrived.wait(10), 'continuation did not reach the real provider body'
                assert captured['before_body'] == old_head
                head = env.records.read('v2_turn_streams', identity)
                assert head.payload['terminal'] is None
                assert head.payload['sequence'] > old_cursor
                assert head.payload['binding'] == old_head.payload['binding']
                assert head.payload['events'][:old_cursor] == old_head.payload['events']
                before_get = app.state.ai_turn_store.events_after(identity)
                async def live_suffix():
                    response = await stream_endpoint(app)(identity, Request({'type': 'http', 'headers': []}),
                        project_id='alpha', after=str(old_cursor))
                    pieces = []
                    iterator = response.body_iterator
                    try:
                        for _ in range(2):
                            pieces.append(await asyncio.wait_for(iterator.__anext__(), .5))
                    finally:
                        await iterator.aclose()
                    return ''.join(pieces)
                suffix = asyncio.run(live_suffix())
                class TextResponse:
                    text = suffix
                live = stream_events(TextResponse())
                assert [kind for _, kind, _ in live] == ['reset', 'delta']
                assert live[0][2] == {'text': complete}
                assert live[1][2] == {'text': tail}
                assert old_cursor < live[0][0] < live[1][0] == head.payload['sequence']
                assert app.state.ai_turn_store.events_after(identity) == before_get
                assert len(calls) == 2
            finally:
                release.set()
            result = continued.result(timeout=25)
        assert result.status_code == 200, result.text
        final = result.json()
        assert final['id'] == identity and final['receipt']['ask']['answer'] == complete + tail
        assert final['receipt']['ask']['model_usage'] == {'input_tokens': 16, 'output_tokens': 7, 'total_tokens': 23}
        assert [citation['n'] for citation in final['receipt']['ask']['citations']] == [1]
        finished = env.records.read('v2_turn_streams', identity)
        assert finished.payload['terminal'] == finished.payload['sequence'] > head.payload['sequence']
        assert env.records.read('v2_turn_frames', identity) is None
        kernel_final = app.state.ai_turn_store.events_after(identity)
        replay = http.get(f'/api/v2/workbench/turns/{identity}/stream?project_id=alpha',
            headers={'Last-Event-ID': str(old_cursor)})
        assert replay.status_code == 200, replay.text
        assert stream_events(replay) == [(finished.payload['terminal'], 'done',
            {'thread_id': final['thread_id'], 'turn': final})]
        assert app.state.ai_turn_store.events_after(identity) == kernel_final
        assert app.state.ai_turn_store.get_request(identity) == frozen
        assert len([row for row in kernel_final if row['type'] == 'model.attempt.dispatched']) == 2
        assert [row for row in kernel_final if row['type'] == 'tool.intent.recorded'][:len(intents)] == intents
        repeated = http.post(f'/api/v2/workbench/turns/{identity}/continue', json={'project_id': 'alpha'},
            headers={'Idempotency-Key': 'continued-stream-explicit'})
        assert repeated.json() == final
        assert env.records.read('v2_turn_streams', identity) == finished
    assert len(calls) == 2 and closed == [True, True]


def test_rejected_continue_reset_never_labels_new_answer_with_previous_done_cursor(env):
    app, _, _, calls, closed, complete = interrupted_app(env, continuation=True)
    with TestClient(app) as http:
        initial = http.post('/api/v2/workbench/turns', json={'project_id': 'alpha', 'text': 'alpha?'},
            headers={'Accept': 'text/event-stream', 'Idempotency-Key': 'reset-denied-original'})
        original = events(initial)[-1][1]
        identity = original['turn']['id']
        old_head = env.records.read('v2_turn_streams', identity)
        old_cursor = old_head.payload['terminal']
        frozen = app.state.ai_turn_store.get_request(identity)
        with env.records.begin() as tx:
            tx.connection.execute("""CREATE TRIGGER reject_continue_stream_reset BEFORE UPDATE
                ON crp_structured_records WHEN NEW.collection='v2_turn_streams'
                AND json_extract(NEW.payload_json,'$.terminal') IS NULL
                BEGIN SELECT RAISE(ABORT,'derived_reset_unavailable'); END""")
            tx.commit()
        continued = http.post(f'/api/v2/workbench/turns/{identity}/continue', json={'project_id': 'alpha'},
            headers={'Idempotency-Key': 'reset-denied-explicit'})
        assert continued.status_code == 200, continued.text
        final = continued.json()
        assert final['id'] == identity
        assert final['receipt']['ask']['answer'] == complete + '接着写完的第三段。'
        assert final['receipt']['ask']['model_usage'] == {'input_tokens': 16, 'output_tokens': 7, 'total_tokens': 23}
        assert app.state.ai_runtime.receipt_for(identity).status == 'completed'
        assert app.state.ai_turn_store.get_immutable_payload(identity, 'product-answer-result-v2')[1]['receipt']['ask']['answer'] == final['receipt']['ask']['answer']
        assert env.records.read('v2_turn_streams', identity) == old_head
        kernel = app.state.ai_turn_store.events_after(identity)
        replay = http.get(f'/api/v2/workbench/turns/{identity}/stream?project_id=alpha',
            headers={'Last-Event-ID': str(old_cursor)})
        assert replay.status_code == 200, replay.text
        assert replay.text == 'event: done\ndata: ' + json.dumps(
            {'thread_id': final['thread_id'], 'turn': final}, ensure_ascii=False) + '\n\n'
        assert env.records.read('v2_turn_streams', identity) == old_head
        assert app.state.ai_turn_store.events_after(identity) == kernel
        assert app.state.ai_turn_store.get_request(identity) == frozen
        assert len([event for event in kernel if event['type'] == 'model.attempt.dispatched']) == 2
        assert env.records.read('v2_turn_frames', identity) is None
    assert len(calls) == 2 and closed == [True, True]


def configure_do_provider(model, final_text):
    def respond(messages, **_kwargs):
        context = json.loads(messages[-1]['content'])
        if 'output' in context:
            return json.dumps({'mode': 'cluster', 'assignments': [{
                'profile_id': 'subagent.worker', 'task': '准备草稿', 'goal': '准备完整草稿',
                'deliverable': '整理稿', 'capabilities': ['document.draft.propose'], 'depends_on': []}]})
        if any(item['capability_id'] == 'agent.list' for item in context.get('capabilities', [])):
            return json.dumps({'type': 'complete', 'summary': final_text}, ensure_ascii=False)
        return json.dumps({'type': 'tool', 'capability_id': 'document.draft.propose',
            'arguments': {'title': '子成果', 'markdown': '真实工具保存的草稿正文。',
                          'final_for': '整理稿'}}, ensure_ascii=False)

    model.handler = respond


def test_real_do_main_and_worker_complete_stream_and_clear_frames_in_result_transaction(do_env):
    from backend.memory_app.v2.turn_frames import frame_text

    http, model = do_env
    records = http.app.state.recognition_service.records
    final_text = '完整汇总段落。' * 60
    arrived, release = Event(), Event()
    main_closed = []
    configure_do_provider(model, final_text)
    provider = model._completion_fn

    def external_provider(**request):
        response = provider(**request)
        context = json.loads(request['messages'][-1]['content'])
        main = any(item['capability_id'] == 'agent.list' for item in context.get('capabilities', []))
        if not main or request.get('stream') is not True:
            return response

        def stream():
            try:
                for index, chunk in enumerate(response):
                    yield chunk
                    if index == 0:
                        arrived.set()
                        assert release.wait(25), 'real Main body barrier was not released'
            finally:
                main_closed.append(True)
        return stream()

    model._completion_fn = external_provider
    # The original runtime and runner execute Main, steward, worker and tools.
    http.app.state.recognition_turn_dispatcher._runtime()
    posted = http.post('/api/v2/workbench/turns', json={
        'project_id': 'project-a', 'intent': 'do', 'text': '准备草稿并汇总'},
        headers={'Idempotency-Key': 'real-do-stream'})
    assert posted.status_code == 200, posted.text
    original = posted.json()
    identity = original['turn']['id']
    try:
        assert arrived.wait(25), 'real Main did not produce its final summary'
        frames = records.read('v2_turn_frames', identity)
        assert frames is not None and frame_text(frames.payload) == final_text
        assert frames.payload['project_id'] == 'project-a'
        assert frames.payload['thread_id'] == original['thread_id']
    finally:
        release.set()

    deadline = monotonic() + 35
    while True:
        product = records.read('v2_turns', identity)
        receipt = product.payload['receipt']['do']
        if receipt['state'] in {'done', 'partial', 'failed'} or monotonic() >= deadline:
            break
        sleep(.05)
    assert receipt['state'] == 'done', receipt
    assert receipt['document_id'] and receipt['progress'] == {'done': 3, 'total': 3}
    assert len(receipt['division']) == 1 and receipt['division'][0]['state'] == 'done'
    store = http.app.state.ai_turn_store
    composition = http.app.state.agent_runtime_composition
    runs = composition.store.list_runs(project_id='project-a')
    main = next(run for run in runs if run.role == 'main')
    worker = next(run for run in runs if run.profile_id == 'subagent.worker')
    before = {run.turn_id: store.events_after(run.turn_id) for run in runs}
    intentions = [event for event in before[worker.turn_id] if event['type'] == 'tool.intent.recorded'
        and event['data']['capability_id'] == 'document.draft.propose']
    assert len(intentions) == 1
    assert len([event for event in before[worker.turn_id] if event['type'] == 'tool.completed'
        and event['correlation']['tool_call_id'] == intentions[0]['correlation']['tool_call_id']]) == 1
    assert before[main.turn_id][-1]['type'] == 'turn.completed'
    assert sum(event['type'] == 'model.attempt.dispatched' for values in before.values()
        for event in values) == len(model.calls)
    assert main_closed == [True]
    drafts = records.list('v2_task_draft_operations')
    assert len(drafts) == 2
    document = http.get(f"/api/recognition/documents/{receipt['document_id']}?project_id=project-a")
    assert document.status_code == 200 and document.json()['markdown'] == final_text
    thread = http.get(f"/api/v2/workbench/threads/{original['thread_id']}?project_id=project-a")
    assert thread.status_code == 200, thread.text
    final = {'thread_id': original['thread_id'], 'turn': thread.json()['turns'][0]}
    assert final['turn']['id'] == identity and final['turn']['receipt']['do']['state'] == 'done'
    saved = {name: records.list(name) for name in
        ('v2_turns', 'v2_task_executions', 'v2_turn_streams', 'v2_turn_frames', 'v2_task_draft_operations')}
    calls_before = len(model.calls)
    replay = http.get(f'/api/v2/workbench/turns/{identity}/stream?project_id=project-a&after=0')
    assert replay.status_code == 200, replay.text
    head = records.read('v2_turn_streams', identity)
    assert head is not None and head.payload['terminal'] == head.payload['sequence'] > 0
    assert records.read('v2_turn_frames', identity) is None
    values = stream_events(replay)
    assert values == [(head.payload['terminal'], 'done', final)]
    assert values[0][2]['turn']['receipt']['do']['document_id'] == receipt['document_id']
    assert values[0][2]['turn']['receipt']['do']['model_usage']['input_tokens'] > 0
    assert {run.turn_id: store.events_after(run.turn_id) for run in runs} == before
    assert len(model.calls) == calls_before and main_closed == [True]
    assert {name: records.list(name) for name in saved} == saved


def test_do_post_stream_waits_for_real_completion_and_get_replay_without_dispatch(do_env):
    http, model = do_env
    configure_do_provider(model, '真实主智能体最终汇总。' * 60)
    http.app.state.recognition_turn_dispatcher._runtime()
    body = {'project_id': 'project-a', 'intent': 'do', 'text': '准备草稿并汇总'}
    headers = {'Accept': 'text/event-stream', 'Idempotency-Key': 'do-post-stream'}
    posted = http.post('/api/v2/workbench/turns', json=body, headers=headers)
    assert posted.status_code == 200, posted.text
    values = events(posted)
    assert values[0][0] == 'started'
    final = values[-1][1]
    assert values[-1][0] == 'done' and final['turn']['receipt']['do']['state'] == 'done'
    assert sum(kind == 'done' for kind, _ in values) == 1
    assert final['turn']['receipt']['do']['division'][0]['state'] == 'done'
    assert final['turn']['receipt']['do']['document_id']
    identity = final['turn']['id']
    assert values[0][1]['turn']['id'] == identity
    assert posted.headers['X-Accel-Buffering'] == 'no' and posted.headers['Vary'] == 'Accept'
    blocks = [block for block in posted.text.split('\n\n') if 'data: ' in block]
    persisted = []
    for block in blocks:
        lines = block.splitlines()
        ids = [line.removeprefix('id: ') for line in lines if line.startswith('id: ')]
        if not ids:
            assert block is blocks[0] and lines[0] == 'event: started'
            continue
        assert len(ids) == 1 and ids[0].isascii() and ids[0].isdigit()
        persisted.append(int(ids[0]))
    assert persisted and persisted == sorted(set(persisted))
    records = http.app.state.recognition_service.records
    head = records.read('v2_turn_streams', identity)
    assert head.payload['terminal'] == persisted[-1]
    assert records.read('v2_turn_frames', identity) is None
    assert len(records.list('v2_task_draft_operations')) == 2
    runs = http.app.state.agent_runtime_composition.store.list_runs(project_id='project-a')
    store = http.app.state.ai_turn_store
    before = {run.turn_id: store.events_after(run.turn_id) for run in runs}
    worker = next(run for run in runs if run.profile_id == 'subagent.worker')
    assert len([event for event in before[worker.turn_id] if event['type'] == 'tool.intent.recorded'
        and event['data']['capability_id'] == 'document.draft.propose']) == 1
    call_count = len(model.calls)
    replay = http.get(f'/api/v2/workbench/turns/{identity}/stream?project_id=project-a&after=0')
    assert replay.status_code == 200 and stream_events(replay) == [(persisted[-1], 'done', final)]
    repeated = http.post('/api/v2/workbench/turns', json=body, headers=headers)
    assert repeated.status_code == 200 and events(repeated) == [('done', final)]
    assert len(model.calls) == call_count
    assert {run.turn_id: store.events_after(run.turn_id) for run in runs} == before


def test_corrupt_derived_do_head_preserves_authoritative_final_transaction(do_env):
    from backend.memory_app.v2.turn_frames import frame_text

    http, model = do_env
    records = http.app.state.recognition_service.records
    final_text = '损坏旁路仍保存真实完工汇总。' * 60
    arrived, release = Event(), Event()
    main_closed = []
    configure_do_provider(model, final_text)
    provider = model._completion_fn

    def external_provider(**request):
        response = provider(**request)
        context = json.loads(request['messages'][-1]['content'])
        main = any(item['capability_id'] == 'agent.list' for item in context.get('capabilities', []))
        if not main or request.get('stream') is not True:
            return response

        def stream():
            try:
                for index, chunk in enumerate(response):
                    yield chunk
                    if index == 0:
                        arrived.set()
                        assert release.wait(25), 'real Main body barrier was not released'
            finally:
                main_closed.append(True)
        return stream()

    model._completion_fn = external_provider
    http.app.state.recognition_turn_dispatcher._runtime()
    posted = http.post('/api/v2/workbench/turns', json={
        'project_id': 'project-a', 'intent': 'do', 'text': '准备草稿并汇总'},
        headers={'Idempotency-Key': 'corrupt-derived-do-head'})
    assert posted.status_code == 200, posted.text
    original = posted.json()
    identity = original['turn']['id']
    try:
        assert arrived.wait(25), 'real Main did not reach its final summary'
        frames = records.read('v2_turn_frames', identity)
        assert frames is not None and frame_text(frames.payload) == final_text
        head = records.read('v2_turn_streams', identity)
        assert head is not None and head.payload['terminal'] is None
        # Corrupt only a derived row in this synthetic SQLite database.
        with records.begin() as tx:
            changed = tx.connection.execute('''UPDATE crp_structured_records
                SET payload_json=? WHERE collection=? AND object_id=? AND revision=?''',
                ('{broken', 'v2_turn_streams', identity, head.revision))
            assert changed.rowcount == 1
            tx.commit()
    finally:
        release.set()

    deadline = monotonic() + 35
    while http.app.state.workbench_tasks and monotonic() < deadline:
        sleep(.05)
    assert not http.app.state.workbench_tasks, 'real background final writer did not finish'
    store = http.app.state.ai_turn_store
    runs = http.app.state.agent_runtime_composition.store.list_runs(project_id='project-a')
    assert len(runs) == 3
    main = next(run for run in runs if run.role == 'main')
    worker = next(run for run in runs if run.profile_id == 'subagent.worker')
    before = {run.turn_id: store.events_after(run.turn_id) for run in runs}
    assert all(values[-1]['type'] == 'turn.completed' for values in before.values())
    intentions = [event for event in before[worker.turn_id] if event['type'] == 'tool.intent.recorded'
        and event['data']['capability_id'] == 'document.draft.propose']
    assert len(intentions) == 1
    assert len([event for event in before[worker.turn_id] if event['type'] == 'tool.completed'
        and event['correlation']['tool_call_id'] == intentions[0]['correlation']['tool_call_id']]) == 1
    assert before[main.turn_id][-1]['type'] == 'turn.completed'
    assert sum(event['type'] == 'model.attempt.dispatched' for values in before.values()
        for event in values) == len(model.calls) == 3
    assert main_closed == [True]
    drafts = records.list('v2_task_draft_operations')
    assert len(drafts) == 2
    product = records.read('v2_turns', identity)
    receipt = product.payload['receipt']['do']
    assert receipt['state'] == 'done', receipt
    assert receipt['document_id'] and receipt['progress'] == {'done': 3, 'total': 3}
    assert len(receipt['division']) == 1 and receipt['division'][0]['state'] == 'done'
    execution = records.read('v2_task_executions', identity)
    assert execution.payload['started'] is True and execution.payload['owner'] is None
    document = http.get(f"/api/recognition/documents/{receipt['document_id']}?project_id=project-a")
    assert document.status_code == 200 and document.json()['markdown'] == final_text
    saved = {name: records.list(name) for name in
        ('v2_turns', 'v2_task_executions', 'v2_turn_frames', 'v2_task_draft_operations')}
    with closing(sqlite3.connect(records.database_path.resolve().as_uri() + '?mode=ro', uri=True)) as read:
        damaged = read.execute('''SELECT payload_json,revision FROM crp_structured_records
            WHERE collection=? AND object_id=?''', ('v2_turn_streams', identity)).fetchone()
    assert damaged == ('{broken', head.revision)
    calls_before = len(model.calls)
    replay = http.get(f'/api/v2/workbench/turns/{identity}/stream?project_id=project-a&after=0')
    assert replay.status_code == 404 and replay.json() == {'detail': 'workbench_not_found'}
    assert len(model.calls) == calls_before and main_closed == [True]
    assert {run.turn_id: store.events_after(run.turn_id) for run in runs} == before
    assert {name: records.list(name) for name in saved} == saved
    with closing(sqlite3.connect(records.database_path.resolve().as_uri() + '?mode=ro', uri=True)) as read:
        assert read.execute('''SELECT payload_json,revision FROM crp_structured_records
            WHERE collection=? AND object_id=?''', ('v2_turn_streams', identity)).fetchone() == damaged


def test_headless_do_started_and_asgi_disconnect_preserve_real_background_execution(do_env):
    http, model = do_env
    records = http.app.state.recognition_service.records
    final_text = '断开订阅后仍由真实后台完成汇总。' * 60
    arrived, release, started = Event(), Event(), Event()
    main_closed, sent, received_disconnect = [], [], []
    control = {}
    configure_do_provider(model, final_text)
    provider = model._completion_fn

    def external_provider(**request):
        response = provider(**request)
        context = json.loads(request['messages'][-1]['content'])
        main = any(item['capability_id'] == 'agent.list' for item in context.get('capabilities', []))
        if not main or request.get('stream') is not True:
            return response

        def stream():
            try:
                # Block the real provider before its first chunk: no derived
                # head exists and the HTTP subscriber must be independent.
                arrived.set()
                assert release.wait(25), 'real Main pre-chunk barrier was not released'
                yield from response
            finally:
                main_closed.append(True)
        return stream()

    model._completion_fn = external_provider
    http.app.state.recognition_turn_dispatcher._runtime()
    body = {'project_id': 'project-a', 'intent': 'do', 'text': '准备草稿并汇总'}
    request = http.build_request('POST', '/api/v2/workbench/turns', json=body,
        headers={'Accept': 'text/event-stream', 'Idempotency-Key': 'headless-disconnect-do'})

    async def subscribe():
        control['disconnect'] = asyncio.Event()
        delivered_body = False

        async def receive():
            nonlocal delivered_body
            if not delivered_body:
                delivered_body = True
                return {'type': 'http.request', 'body': request.content, 'more_body': False}
            await control['disconnect'].wait()
            received_disconnect.append('http.disconnect')
            return {'type': 'http.disconnect'}

        async def send(message):
            sent.append(dict(message))
            if message['type'] == 'http.response.body' and b'event: started\n' in message['body']:
                started.set()

        await http.app({'type': 'http', 'asgi': {'version': '3.0', 'spec_version': '2.3'},
            'http_version': '1.1', 'method': 'POST', 'scheme': 'http',
            'path': '/api/v2/workbench/turns', 'raw_path': b'/api/v2/workbench/turns',
            'query_string': b'', 'root_path': '',
            'headers': [(name.lower(), value) for name, value in request.headers.raw],
            'client': ('testclient', 50000), 'server': ('testserver', 80), 'state': {}}, receive, send)

    subscription = http.portal.start_task_soon(subscribe)
    deadline = monotonic() + 25
    try:
        while not started.is_set() and not subscription.done() and monotonic() < deadline:
            sleep(.01)
        if subscription.done():
            subscription.result()
        assert started.is_set(), {'response_status':
            [value['status'] for value in sent if value['type'] == 'http.response.start'],
            'event_types': [value['type'] for value in sent],
            'body_bytes': sum(len(value['body']) for value in sent if value['type'] == 'http.response.body'),
            'future_done': subscription.done(), 'main_entered': arrived.is_set(), 'calls': len(model.calls)}
        assert arrived.wait(max(0, deadline - monotonic())), 'real Main did not reach its pre-chunk barrier'
        assert [value['status'] for value in sent if value['type'] == 'http.response.start'] == [200]
        content = b''.join(value['body'] for value in sent if value['type'] == 'http.response.body').decode()
        blocks = [block for block in content.split('\n\n') if block.startswith('event: ')]
        assert len(blocks) == 1 and blocks[0].splitlines()[0] == 'event: started'
        assert not any(line.startswith('id: ') for line in blocks[0].splitlines())
        initial = json.loads(next(line.removeprefix('data: ') for line in blocks[0].splitlines()
            if line.startswith('data: ')))
        identity = initial['turn']['id']
        product = records.read('v2_turns', identity)
        assert initial == {'thread_id': product.payload['thread_id'], 'turn':
            {'id': identity, 'intent': 'do', 'user_text': body['text']}}
        assert product.payload['receipt']['do']['state'] == 'running'
        assert records.read('v2_turn_streams', identity) is None
        assert records.read('v2_turn_frames', identity) is None
        store = http.app.state.ai_turn_store
        runs = http.app.state.agent_runtime_composition.store.list_runs(project_id='project-a')
        before = {run.turn_id: store.events_after(run.turn_id) for run in runs}
        saved = {name: records.list(name) for name in
            ('v2_turns', 'v2_task_executions', 'v2_turn_streams', 'v2_turn_frames',
             'v2_task_draft_operations', 'v2_turn_requests')}
        calls_before = len(model.calls)
        missing = http.get(f'/api/v2/workbench/turns/{identity}/stream?project_id=project-a&after=0')
        assert missing.status_code == 404 and missing.json() == {'detail': 'workbench_not_found'}
        assert {name: records.list(name) for name in saved} == saved
        assert {run.turn_id: store.events_after(run.turn_id) for run in runs} == before
        assert len(model.calls) == calls_before == 3
        http.portal.call(control['disconnect'].set)
        subscription.result(timeout=max(.001, deadline - monotonic()))
        assert subscription.done() and not subscription.cancelled() and received_disconnect
        assert http.app.state.workbench_tasks and not release.is_set() and main_closed == []
        assert records.read('v2_turn_streams', identity) is None
        disconnected_messages = list(sent)
    finally:
        if not subscription.done() and 'disconnect' in control:
            http.portal.call(control['disconnect'].set)
        release.set()

    deadline = monotonic() + 35
    while http.app.state.workbench_tasks and monotonic() < deadline:
        sleep(.05)
    assert not http.app.state.workbench_tasks, 'protected real TaskDo did not finish'
    product = records.read('v2_turns', identity)
    receipt = product.payload['receipt']['do']
    assert receipt['state'] == 'done' and receipt['progress'] == {'done': 3, 'total': 3}
    assert receipt['document_id'] and len(receipt['division']) == 1 and receipt['division'][0]['state'] == 'done'
    execution = records.read('v2_task_executions', identity)
    assert execution.payload['started'] is True and execution.payload['owner'] is None
    before = {run.turn_id: store.events_after(run.turn_id) for run in runs}
    assert len(runs) == 3 and all(values[-1]['type'] == 'turn.completed' for values in before.values())
    worker = next(run for run in runs if run.profile_id == 'subagent.worker')
    intentions = [event for event in before[worker.turn_id] if event['type'] == 'tool.intent.recorded'
        and event['data']['capability_id'] == 'document.draft.propose']
    assert len(intentions) == 1
    assert len([event for event in before[worker.turn_id] if event['type'] == 'tool.completed'
        and event['correlation']['tool_call_id'] == intentions[0]['correlation']['tool_call_id']]) == 1
    assert sum(event['type'] == 'model.attempt.dispatched' for values in before.values()
        for event in values) == len(model.calls) == 3
    assert main_closed == [True] and len(records.list('v2_task_draft_operations')) == 2
    assert sent == disconnected_messages and subscription.done()
    document = http.get(f"/api/recognition/documents/{receipt['document_id']}?project_id=project-a")
    assert document.status_code == 200 and document.json()['markdown'] == final_text
    thread = http.get(f"/api/v2/workbench/threads/{initial['thread_id']}?project_id=project-a")
    assert thread.status_code == 200
    final = {'thread_id': initial['thread_id'], 'turn': thread.json()['turns'][0]}
    assert final['turn']['id'] == identity and final['turn']['receipt']['do']['state'] == 'done'
    head = records.read('v2_turn_streams', identity)
    assert head.payload['terminal'] == head.payload['sequence'] > 0
    assert records.read('v2_turn_frames', identity) is None
    saved = {name: records.list(name) for name in saved}
    replay = http.get(f'/api/v2/workbench/turns/{identity}/stream?project_id=project-a&after=0')
    assert replay.status_code == 200 and stream_events(replay) == [(head.payload['terminal'], 'done', final)]
    assert {name: records.list(name) for name in saved} == saved
    assert {run.turn_id: store.events_after(run.turn_id) for run in runs} == before
    assert len(model.calls) == 3 and main_closed == [True] and sent == disconnected_messages
