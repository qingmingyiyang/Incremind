import asyncio
from concurrent.futures import ThreadPoolExecutor
import os
from pathlib import Path
from shutil import copyfile
import sqlite3
from threading import Event
import time
from uuid import uuid4

import anyio.to_thread
from fastapi.testclient import TestClient
import pytest
from starlette.concurrency import run_in_threadpool

from backend.memory_app.v2.turn_message_order import ACCEPTANCES, COLLECTION, HEADS


@pytest.fixture(scope='module')
def installed():
    root = Path(os.environ['CHRIPTMAS_APP_ROOT']).resolve(strict=True) / 'message-order'
    root.mkdir()
    previous = os.environ['CHRIPTMAS_APP_ROOT']
    os.environ['CHRIPTMAS_APP_ROOT'] = str(root)
    config = root / 'config/settings.toml'
    config.parent.mkdir()
    copyfile(Path(__file__).parents[3] / 'config/settings.toml.example', config)
    from backend.memory_app.app import create_app
    app = create_app(runtime_root=root)
    models = app.state.recognition_models
    models.update('generation', {'expected_revision': models.public()['generation']['revision'],
        'base_url': 'https://127.0.0.2:9/v1', 'model': 'gpt-4o-mini',
        'api_key': 'isolated-no-wire-placeholder', 'enabled': True, 'allow_remote': False})
    try:
        with TestClient(app) as client:
            created = client.post('/api/v2/projects', json={'name': '并发受理合成项目'})
            assert created.status_code == 200
            yield app, client, created.json()['id']
    finally:
        os.environ['CHRIPTMAS_APP_ROOT'] = previous


def test_actual_acceptance_reserves_before_route_and_does_not_skip_a_pending_message(installed):
    app, client, project = installed
    records, learning = app.state.recognition_records, app.state.product_steer_learning
    first = client.post('/api/v2/workbench/turns', json={'project_id': project, 'intent': 'ask',
        'text': '#并发受理合成项目 无匹配合成问题' + uuid4().hex},
        headers={'Idempotency-Key': 'message-order-first'})
    assert first.status_code == 200
    original = first.json()['turn']
    assert original['receipt']['ask']['no_match'] is True
    original_order = records.read(COLLECTION, original['id'])
    first_slot = records.read(ACCEPTANCES, original['id'])
    assert original_order.payload['sequence'] == first_slot.payload['sequence'] == 1
    assert records.read('v2_turn_requests', 'message-order-first').payload['result']['turn']['id'] == original['id']

    started, release = Event(), Event()
    state = {}
    def occupy_worker():
        started.set()
        if not release.wait(timeout=20):
            raise TimeoutError('message_order_worker_wait_expired')
    async def hold_worker():
        limiter = anyio.to_thread.current_default_thread_limiter()
        state.update(limiter=limiter, previous=limiter.total_tokens)
        limiter.total_tokens = 1
        state['task'] = asyncio.create_task(run_in_threadpool(occupy_worker))
        while not started.is_set():
            await asyncio.sleep(0.001)
    async def release_worker():
        release.set()
        try:
            await state['task']
        finally:
            state['limiter'].total_tokens = state['previous']

    pending_body = {'project_id': project, 'thread_id': original['thread_id'], 'intent': 'auto',
        'text': '#并发受理合成项目 先记住预算为三十元，再按步骤说明如何核对'}
    next_body = {'project_id': project, 'thread_id': original['thread_id'], 'intent': 'inspiration',
        'text': '#并发受理合成项目 以后请使用简短列表'}
    client.portal.call(hold_worker)
    try:
        with ThreadPoolExecutor(max_workers=2) as pool:
            pending = pool.submit(client.post, '/api/v2/workbench/turns', json=pending_body,
                headers={'Idempotency-Key': 'message-order-pending'})
            deadline = time.monotonic() + 5
            while time.monotonic() < deadline:
                slots = [row for row in records.list(ACCEPTANCES)
                    if row.payload.get('request_key') == 'message-order-pending']
                if slots:
                    break
                time.sleep(0.01)
            assert len(slots) == 1 and not pending.done()
            middle = slots[0]
            assert middle.payload['sequence'] == 2 and middle.payload['previous_id'] == original['id']
            assert records.read(COLLECTION, middle.object_id) is None
            following = pool.submit(client.post, '/api/v2/workbench/turns', json=next_body,
                headers={'Idempotency-Key': 'message-order-next'}).result(timeout=5)
            assert following.status_code == 200
            current = following.json()['turn']
            current_order = records.read(COLLECTION, current['id'])
            assert current_order.payload['sequence'] == 3 and current_order.payload['previous_id'] == middle.object_id
            assert records.read(HEADS, original['thread_id']).payload['public_id'] == current['id']
            with learning.freshness._read_scope() as view:
                assert learning.order.pair(view, current['id']) is None
            assert not any(row.payload.get('public_turn_id') == current['id']
                for row in records.list('v2_turn_steer_feedback'))
            client.portal.call(release_worker)
            result = pending.result(timeout=15)
            assert result.status_code == 200
            assert result.json()['turn']['id'] == middle.object_id
            assert records.read(COLLECTION, middle.object_id).payload['sequence'] == 2
            assert records.read(HEADS, original['thread_id']).payload['public_id'] == current['id']
    finally:
        release.set()
        if state['limiter'].total_tokens != state['previous']:
            client.portal.call(release_worker)
    before = tuple((row.object_id, row.revision, row.payload) for row in records.list(ACCEPTANCES))
    replay = client.post('/api/v2/workbench/turns', json=pending_body,
        headers={'Idempotency-Key': 'message-order-pending'})
    assert replay.status_code == 200 and replay.json() == result.json()
    assert before == tuple((row.object_id, row.revision, row.payload) for row in records.list(ACCEPTANCES))
    learning.refresh()
    assert not any(row.payload.get('public_turn_id') == current['id']
        for row in records.list('v2_turn_steer_feedback'))
    assert all(not event['type'].startswith('model.attempt.')
        for request_id in (original['id'], middle.object_id)
        for event in app.state.ai_turn_store.events_after(request_id))
    with sqlite3.connect(Path(app.state.ai_turn_store._path).resolve().as_uri() + '?mode=ro', uri=True) as connection:
        assert connection.execute("SELECT COUNT(*) FROM ai_turn_events WHERE json_extract(event_json,'$.type') = ?",
            ('model.attempt.dispatched',)).fetchone()[0] == 0


def test_failed_accepted_request_keeps_its_sequence_and_replay_does_not_reserve_again(installed):
    app, client, _ = installed
    records, learning = app.state.recognition_records, app.state.product_steer_learning
    project = client.post('/api/v2/projects', json={'name': '并发失败合成项目'}).json()['id']
    original = client.post('/api/v2/workbench/turns', json={'project_id': project, 'intent': 'ask',
        'text': '#并发失败合成项目 无匹配问题'}, headers={'Idempotency-Key': 'message-failed-first'})
    assert original.status_code == 200 and original.json()['turn']['receipt']['ask']['no_match'] is True
    thread = original.json()['thread_id']
    body = {'project_id': project, 'thread_id': thread, 'intent': 'inspiration',
        'text': '#并发失败合成项目 无效接着写请求', 'continue_from': 'invalid-document'}
    failed = client.post('/api/v2/workbench/turns', json=body,
        headers={'Idempotency-Key': 'message-failed-middle'})
    assert failed.status_code == 400
    middle = next(row for row in records.list(ACCEPTANCES) if row.payload['request_key'] == 'message-failed-middle')
    assert middle.payload['sequence'] == 2 and records.read(COLLECTION, middle.object_id) is None
    following = client.post('/api/v2/workbench/turns', json={'project_id': project, 'thread_id': thread,
        'intent': 'inspiration', 'text': '#并发失败合成项目 以后请列具体步骤'},
        headers={'Idempotency-Key': 'message-failed-next'})
    assert following.status_code == 200
    current = following.json()['turn']['id']
    assert records.read(COLLECTION, current).payload['previous_id'] == middle.object_id
    with learning.freshness._read_scope() as view:
        assert learning.order.pair(view, current) is None
    before = tuple((row.object_id, row.revision, row.payload) for row in records.list(ACCEPTANCES))
    replay = client.post('/api/v2/workbench/turns', json=body,
        headers={'Idempotency-Key': 'message-failed-middle'})
    assert replay.status_code == 400
    assert before == tuple((row.object_id, row.revision, row.payload) for row in records.list(ACCEPTANCES))
    learning.refresh()
    assert not any(row.payload.get('public_turn_id') == current
        for row in records.list('v2_turn_steer_feedback'))
