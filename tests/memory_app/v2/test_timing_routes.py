"""Request integration: timing remains an internal sidecar, including background work."""
import time
import asyncio

from tests.memory_app.v2.test_workbench_ask import env as ask_env, publish, ask
from tests.memory_app.v2.test_workbench_remember import env as remember_env, post, wait
from tests.memory_app.v2.test_workbench_do import env as do_env
from core.storage_provider.sqlite_uow import SQLiteStructuredRecordUnitOfWork


def timing(records, identity):
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        row = records.read('v2_turn_timings', identity)
        if row is not None:
            return row.payload
        time.sleep(.01)
    raise AssertionError('Timing sidecar was not persisted')


def finish_background(client):
    # Business execution has its own bounded wait. The separate five-second
    # timing assertion below still checks persistence after business completion.
    async def finish():
        tasks = tuple(client.app.state.workbench_tasks)
        if tasks:
            done, pending = await asyncio.wait(tasks, timeout=90)
            assert not pending, 'workbench business tasks did not finish'
            for task in done:
                task.result()
    client.portal.call(finish)


def test_ask_timing_is_durable_but_not_in_public_response(ask_env):
    publish(ask_env)
    response = ask(ask_env)
    assert response.status_code == 200, response.text
    body = response.json()
    assert set(body) == {'thread_id', 'turn'}
    assert set(body['turn']) == {'id', 'thread_id', 'intent', 'user_text', 'created_at', 'receipt'}
    assert 'timing' not in response.text
    measured = timing(ask_env.records, body['turn']['id'])
    assert measured['operation'] == 'ask'
    assert measured['connection_count'] > 0
    assert measured['statement_count'] >= measured['connection_count']
    assert measured['stages_ms']['persist'] > 0
    assert ask_env.records.read('v2_turns', body['turn']['id']).payload.get('timings') is None


def test_remember_timing_waits_for_background_completion(remember_env):
    result = post(remember_env)
    turn = wait(remember_env, result)
    assert turn['receipt']['remember']['state'] == 'done'
    measured = timing(remember_env.records, turn['id'])
    assert measured['operation'] == 'remember'
    assert measured['connection_count'] > 0
    assert measured['stages_ms']['persist'] > 0
    assert remember_env.model.calls >= 2
    assert 'timing' not in turn


def test_library_list_has_separate_timing_without_response_changes(ask_env):
    publish(ask_env)
    before = len(ask_env.records.list('v2_turn_timings'))
    response = ask_env.http.get('/api/v2/library/insights', params={'project_id': 'alpha'})
    assert response.status_code == 200, response.text
    assert set(response.json()) == {'items', 'counts'}
    assert len(ask_env.records.list('v2_turn_timings')) == before + 1


def test_sidecar_write_failure_keeps_answer_and_business_terminal(ask_env, monkeypatch, caplog):
    publish(ask_env)
    original = SQLiteStructuredRecordUnitOfWork.put
    def put(self, collection, *args, **kwargs):
        if collection == 'v2_turn_timings':
            raise OSError('private injected timing write failure')
        return original(self, collection, *args, **kwargs)
    monkeypatch.setattr(SQLiteStructuredRecordUnitOfWork, 'put', put)
    response = ask(ask_env)
    assert response.status_code == 200, response.text
    body = response.json()
    assert body['turn']['receipt']['ask']['answer'] == 'Synthetic answer'
    assert ask_env.records.read('v2_turns', body['turn']['id']) is not None
    assert 'timing_write_failed' in caplog.text
    assert 'private injected' not in caplog.text


def test_task_submission_records_counts_without_response_fields(do_env, monkeypatch):
    from backend.memory_app.v2.task_do import TaskDo
    original = TaskDo.advance
    async def delayed(self, turn_id):
        await asyncio.sleep(.05)
        return await original(self, turn_id)
    monkeypatch.setattr(TaskDo, "advance", delayed)
    client, _models = do_env
    response = client.post('/api/v2/workbench/turns', json={
        'project_id': 'project-a', 'intent': 'do', 'text': '写一段总结'})
    assert response.status_code == 200, response.text
    body = response.json()
    records = client.app.state.recognition_service.records
    finish_background(client)
    measured = timing(records, body['turn']['id'])
    assert records.read('v2_turns', body['turn']['id']).payload['receipt']['do']['state'] in {'done', 'partial', 'failed'}
    assert measured['total_ms'] >= 50
    assert measured['operation'] == 'task'
    assert measured['connection_count'] > 0
    assert measured['total_ms'] > 0
    assert 'timing' not in response.text


def test_idempotency_claim_and_terminal_check_are_counted(tmp_path):
    from backend.memory_app.v2.turn_execution import TurnExecutionService
    from core.storage_provider import SQLiteStructuredRecordStore
    records = SQLiteStructuredRecordStore(tmp_path / 'counts.db')
    async def execute(body, **kwargs):
        records.list('items')
        return {'answer': 'synthetic'}
    executions = TurnExecutionService(records, execute, instance='synthetic-instance')
    assert asyncio.run(executions.run({'intent': 'ask'}, 'count-once')) == {'answer': 'synthetic'}
    first = records.list('v2_turn_timings')[0]
    assert first.payload['connection_count'] == 1
    assert records.read('v2_turn_requests', 'count-once').payload['state'] == 'completed'
    assert asyncio.run(executions.run({'intent': 'ask'}, 'count-once')) == {'answer': 'synthetic'}
    rows = records.list('v2_turn_timings')
    assert len(rows) == 2
    replay = next(row for row in rows if row.object_id != first.object_id)
    assert replay.payload['connection_count'] == 1
    assert records.read('v2_turn_timings', first.object_id).revision == first.revision


def test_detached_request_waits_for_business_task_before_timing_snapshot(tmp_path):
    from backend.memory_app.v2.turn_execution import TurnExecutionService
    from core.storage_provider import SQLiteStructuredRecordStore
    from core.storage_provider.observability import current_observation
    records = SQLiteStructuredRecordStore(tmp_path / 'detached.db')
    async def scenario():
        entered, release = asyncio.Event(), asyncio.Event()
        async def execute(body, **kwargs):
            current_observation().set_turn_id('turn-detached')
            entered.set()
            await release.wait()
            records.list('items')
            return {'answer': 'synthetic'}
        executions = TurnExecutionService(records, execute, instance='synthetic-instance')
        request = asyncio.create_task(executions.run({'intent': 'ask'}, 'detached'))
        await entered.wait()
        request.cancel()
        try:
            await request
        except asyncio.CancelledError:
            pass
        assert records.read('v2_turn_timings', 'turn-detached') is None
        tasks = tuple(executions.tasks.values())
        release.set()
        await asyncio.gather(*tasks)
    asyncio.run(scenario())
    measured = records.read('v2_turn_timings', 'turn-detached').payload
    assert measured['connection_count'] == 1
    assert records.read('v2_turn_requests', 'detached').payload['state'] == 'completed'


def test_lease_worker_inherits_sql_counter_without_model_worker_pool(tmp_path):
    from backend.memory_app.workspace_intake import _run_lease_operation
    from backend.memory_app.v2.turn_timings import turn_timing
    from core.storage_provider import SQLiteStructuredRecordStore
    records = SQLiteStructuredRecordStore(tmp_path / 'lease-counts.db')
    async def scenario():
        with turn_timing(records, 'remember', turn_id='turn-lease'):
            await _run_lease_operation(records.list, 'items')
    asyncio.run(scenario())
    measured = records.read('v2_turn_timings', 'turn-lease').payload
    assert measured['connection_count'] == 1
    assert measured['statement_count'] > 0


def test_favorites_keep_independent_turn_timings(remember_env, monkeypatch):
    from tests.memory_app.v2.test_favorites import configure, post as favorite_post, read
    from backend.memory_app import workspace_bilibili_media
    monkeypatch.setattr(workspace_bilibili_media, 'read_bilibili_media',
        lambda url, root, **kwargs: {'source_text': '原文证据', 'title': '视频',
            'canonical_url': url, 'acquisition_method': 'official_subtitle', 'content_kind': 'video'})
    configure(remember_env, 2)
    response = favorite_post(remember_env)
    assert response.status_code == 200, response.text
    rows = read(remember_env, response.json())
    assert len(rows) == 2
    finish_background(remember_env.http)
    for turn in rows:
        measured = timing(remember_env.records, turn['id'])
        assert measured['turn_id'] == turn['id']
        assert measured['operation'] == 'remember'
        assert measured['connection_count'] > 0
        assert measured['stages_ms']['persist'] > 0
        assert remember_env.records.read('v2_turns', turn['id']).payload['receipt']['remember']['state'] == 'done'
