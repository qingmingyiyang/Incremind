import asyncio

import pytest
from fastapi import HTTPException

from backend.memory_app.v2.turn_execution import REQUESTS, TurnExecutionService
from backend.memory_app.v2.workbench_transport import SSEDelivery, routed_response, sse_event
from core.storage_provider import SQLiteStructuredRecordStore


async def collect(response):
    return [value async for value in response.body_iterator]


@pytest.mark.parametrize('control,payload', [('reset', {'text': ''}), ('status', {'state': 'running'})])
def test_mixed_delta_batches_preserve_reset_order_and_last_displayed_cursor(control, payload):
    async def check():
        delivery = SSEDelivery()

        async def produce(started, delta):
            delivery.frame(1, 'started', {'thread_id': 'thread-pressure', 'turn': {'id': 'turn-pressure', 'intent': 'ask'}})
            delivery.frame(2, 'delta', {'text': '旧正文'})
            delivery.frame(3, control, payload)
            delivery.frame(4, 'delta', {'text': '新正文'})
            delivery.frame(5, 'status', {'state': 'running'})
            raise HTTPException(409, 'turn_interrupted')

        response = delivery.response(produce)
        await asyncio.sleep(0)
        await asyncio.sleep(0)
        iterator = response.body_iterator
        values = [await asyncio.wait_for(anext(iterator), 2) for _ in range(4)]
        await iterator.aclose()
        assert values == [sse_event('started', {'thread_id': 'thread-pressure',
            'turn': {'id': 'turn-pressure', 'intent': 'ask'}}, 1),
            sse_event('delta', {'text': '旧正文'}, 2), sse_event(control, payload, 3),
            sse_event('delta', {'text': '新正文'}, 4)]
        assert delivery.queue.maxsize == 4

    asyncio.run(check())


def test_contiguous_delta_chunks_still_coalesce_before_the_next_control_frame():
    async def check():
        delivery = SSEDelivery()

        async def produce(started, delta):
            delivery.frame(1, 'started', {'thread_id': 'thread-pressure', 'turn': {'id': 'turn-pressure', 'intent': 'ask'}})
            delivery.frame(2, 'delta', {'text': '第一段'})
            delivery.frame(3, 'delta', {'text': '第二段'})
            delivery.frame(4, 'status', {'state': 'running'})
            raise HTTPException(409, 'turn_interrupted')

        response = delivery.response(produce)
        await asyncio.sleep(0)
        await asyncio.sleep(0)
        values = await asyncio.wait_for(collect(response), 2)
        assert values == [sse_event('started', {'thread_id': 'thread-pressure',
            'turn': {'id': 'turn-pressure', 'intent': 'ask'}}, 1),
            sse_event('delta', {'text': '第一段第二段'}, 3),
            sse_event('status', {'state': 'running'}, 4),
            sse_event('error', {'code': 'turn_interrupted'})]

    asyncio.run(check())


@pytest.mark.parametrize('routed', [False, True])
def test_queue_overflow_detaches_only_delivery_and_keeps_owned_sql_operation(tmp_path, routed):
    async def check():
        records = SQLiteStructuredRecordStore(tmp_path / 'pressure.sqlite3')
        entered, release = asyncio.Event(), asyncio.Event()
        invocations = []

        async def execute(body, *, request_key, on_started, on_delta, on_frame, on_plan=None):
            invocations.append(request_key)
            if on_plan is not None:
                await on_plan('ask')
            on_frame(1, 'started', {'thread_id': 'thread-pressure', 'turn': {'id': request_key, 'intent': 'ask'}})
            for sequence in range(2, 8):
                on_frame(sequence, 'status', {'state': 'running'})
            entered.set()
            await release.wait()
            # 此处只验证原SQLite操作的存活及失败收口，不构造模型或原生成功。
            with records.begin() as tx:
                tx.put('transport_effects', request_key, {'released': True}, expected_revision=0)
                tx.commit()
            raise HTTPException(409, 'turn_interrupted')

        body, key = {'project_id': 'pressure', 'text': '只检查传输生命周期'}, 'request-pressure'
        service = TurnExecutionService(records, execute, instance='pressure-instance')
        if routed:
            response = await routed_response(service, body, key, 'text/event-stream')
        else:
            delivery = SSEDelivery(records)
            response = delivery.response(lambda started, delta, frame: service.run(
                body, key, on_started=started, on_delta=delta, on_frame=frame))
        await asyncio.wait_for(entered.wait(), 2)
        await asyncio.sleep(0)
        values = await asyncio.wait_for(collect(response), 2)
        assert values == [sse_event('started', {'thread_id': 'thread-pressure',
            'turn': {'id': key, 'intent': 'ask'}}, 1),
            *(sse_event('status', {'state': 'running'}, sequence) for sequence in range(2, 5))]
        assert records.read(REQUESTS, key).payload['state'] == 'running'
        assert records.read('transport_effects', key) is None
        assert service.tasks and all(not task.done() for task in service.tasks.values())
        with pytest.raises(HTTPException) as duplicate:
            await service.run(body, key)
        assert duplicate.value.status_code == 409 and duplicate.value.detail == 'turn_in_progress'
        tasks = tuple(service.tasks.values())
        release.set()
        await asyncio.gather(*tasks, return_exceptions=True)
        assert records.read('transport_effects', key).payload == {'released': True}
        assert records.read(REQUESTS, key).payload['state'] == 'failed'
        with pytest.raises(HTTPException) as replay:
            await service.run(body, key)
        assert replay.value.detail == 'turn_interrupted'
        assert invocations == [key]

    asyncio.run(check())
