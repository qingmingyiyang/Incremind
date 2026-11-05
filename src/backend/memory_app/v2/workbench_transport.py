"""HTTP content negotiation and SSE delivery; no generation or persistence."""
import asyncio
import json
import re
from time import monotonic
from threading import Lock

from fastapi import HTTPException
from starlette.responses import StreamingResponse
from core.storage_provider.connection_scope import create_scoped_task
from starlette.responses import JSONResponse
from .turn_execution import safe_failure


def sse_event(event, value, sequence=None):
    return ('event: ' + event + '\n'
        + (('id: ' + str(sequence) + '\n') if sequence is not None else '')
        + 'data: ' + json.dumps(value, ensure_ascii=False) + '\n\n')


def read_stream_response(snapshot, after, *, sleep=asyncio.sleep, clock=monotonic, project_result=None,
                         uncommitted_visible=None):
    """A read-only subscriber; snapshot must never execute or recover a Turn."""
    initial = snapshot(after)
    async def deliver():
        cursor, state, last_heartbeat = after, initial, clock()
        while True:
            values, finished = state
            for sequence, event, value in values:
                if event in {'delta', 'reset'} and uncommitted_visible is not None:
                    if not await asyncio.to_thread(uncommitted_visible):
                        continue
                if event == 'done' and project_result is not None:
                    value = project_result(value)
                yield sse_event(event, value, sequence)
                if sequence is not None:
                    cursor = sequence
            if finished:
                return
            await sleep(1)
            if clock() - last_heartbeat >= 15:
                yield ': keep-alive\n\n'
                last_heartbeat = clock()
            state = snapshot(cursor)
    return StreamingResponse(deliver(), media_type='text/event-stream',
        headers={'Cache-Control': 'no-store', 'X-Accel-Buffering': 'no'})


def negotiate_turn_response(accept, *, allow_stream):
    """RFC 9110: most specific matching range determines each offer's quality.

    Equal weighted offers prefer the existing JSON representation. A specific
    q=0 exclusion overrides a positive wildcard. Unknown media parameters do
    not match these representations; SSE exposes charset=utf-8.
    """
    if not accept:
        return "json"
    ranges = []
    for entry in accept.split(","):
        parts = [part.strip() for part in entry.split(";")]
        media = parts[0].lower()
        quality, params, after_q = 1.0, {}, False
        for parameter in parts[1:]:
            name, sep, value = parameter.partition("=")
            name, value = name.strip().lower(), value.strip().strip('"').lower()
            if name == "q":
                if not re.fullmatch(r"(?:0(?:\.\d{0,3})?|1(?:\.0{0,3})?)", value):
                    raise HTTPException(400, "invalid_accept_quality")
                quality, after_q = float(value), True
            elif sep and not after_q:
                params[name] = value
        ranges.append((media, quality, params))
    def quality(media, offered_params):
        major = media.split("/", 1)[0]
        matches = []
        for value, weight, params in ranges:
            if value not in {media, major + "/*", "*/*"} or any(offered_params.get(k) != v for k, v in params.items()):
                continue
            specificity = 2 if value == media else 1 if value == major + "/*" else 0
            matches.append(((specificity, len(params)), weight))
        if not matches:
            return 0
        rank = max(item[0] for item in matches)
        return max(weight for spec, weight in matches if spec == rank)
    json_q = quality("application/json", {})
    sse_q = quality("text/event-stream", {"charset": "utf-8"}) if allow_stream else 0
    if max(json_q, sse_q) <= 0:
        raise HTTPException(406, "turn_representation_not_acceptable")
    return "sse" if sse_q > json_q else "json"


class SSEDelivery:
    """Coalesce worker deltas with a bounded delivery queue; detach safely."""
    def __init__(self, records=None, *, project_result=None, uncommitted_visible=None):
        self.loop = asyncio.get_running_loop()
        self.queue = asyncio.Queue(maxsize=4)
        self.lock = Lock()
        self.pending = None
        self.active = True
        self.records = records
        self.project_result = project_result
        self.uncommitted_visible = uncommitted_visible

    def started(self, value):
        self._publish("started", value)

    def delta(self, text, *, sequence=None):
        with self.lock:
            if not self.active:
                return
            part = text.get('part') if isinstance(text, dict) else None
            value = text['text'] if isinstance(text, dict) else text
            batch = self.pending
            if batch is None:
                batch = {'parts': {}, 'sequence': None}
                self.pending = batch
                self._schedule('delta', batch)
            batch['parts'][part] = batch['parts'].get(part, '') + value
            if part is None:
                batch['sequence'] = sequence

    def frame(self, sequence, event, value):
        if event == 'delta':
            self.delta(value if 'part' in value else value['text'], sequence=sequence)
        else:
            self._publish(event, value, sequence=sequence)

    def _publish(self, event, value, *, sequence=None, committed=False):
        with self.lock:
            if not self.active:
                return
            # 控制帧结束当前合并批次，后续正文不能越过它推进游标。
            self.pending = None
            self._schedule(event, value, sequence=sequence, committed=committed)

    def _schedule(self, event, value, *, sequence=None, committed=False):
        def put():
            with self.lock:
                if not self.active:
                    return
                try:
                    self.queue.put_nowait((event, value, sequence, committed))
                except asyncio.QueueFull:
                    # 排完已接纳通知后结束交付，由原持久游标继续读取。
                    self.active, self.pending = False, None
        self.loop.call_soon_threadsafe(put)

    def response(self, execute):
        async def run():
            try:
                result = await (execute(self.started, self.delta, self.frame) if self.records is not None
                    else execute(self.started, self.delta))
                # All thread-safe frame notifications precede the terminal batch.
                await asyncio.sleep(0)
                from .turn_frames import STREAMS
                head = self.records.read(STREAMS, result['turn']['id']) if self.records is not None else None
                terminal = head.payload if head is not None and head.payload.get('terminal') is not None else None
                if terminal is not None:
                    from .turn_frames import stream_status
                    for event in terminal['events']:
                        if event['kind'] == 'reset' and event.get('source') == 'terminal_partial':
                            self._publish('reset', {'text': result['turn']['receipt']['ask']['partial']},
                                sequence=event['sequence'], committed=True)
                        elif (event['kind'] == 'status' and event['state'] in {'completed', 'interrupted'}
                                and event['sequence'] > terminal['terminal'] - 3):
                            self._publish('status', stream_status(event), sequence=event['sequence'])
                self._publish('done', result, sequence=terminal['terminal'] if terminal is not None else None)
            except Exception as error:
                self._publish("error", {"code": safe_failure(error)})
        task = create_scoped_task(run())
        async def deliver():
            started = None
            try:
                while True:
                    if not self.active and self.queue.empty():
                        return
                    try:
                        event, value, sequence, committed = await asyncio.wait_for(self.queue.get(), 15)
                    except TimeoutError:
                        yield ": keep-alive\n\n"
                        continue
                    if event == 'started' and self.uncommitted_visible is not None:
                        started = value.get('turn')
                    if event == "delta":
                        with self.lock:
                            batch = value
                            if self.pending is batch:
                                self.pending = None
                            value, sequence = batch['parts'], batch['sequence']
                        if self.uncommitted_visible is not None and not await asyncio.to_thread(self.uncommitted_visible, started):
                            continue
                        for part, text in value.items():
                            data = {'text':text, **({'part':part} if part is not None else {})}
                            yield sse_event('delta', data, sequence if part is None else None)
                        continue
                    if (event == 'reset' and not committed and self.uncommitted_visible is not None
                            and not await asyncio.to_thread(self.uncommitted_visible, started)):
                        continue
                    if event == 'done' and self.project_result is not None:
                        value = self.project_result(value)
                    yield sse_event(event, value, sequence)
                    if event in {"done", "error"}:
                        break
            finally:
                with self.lock:
                    self.active, self.pending = False, None
                # Cancelling the delivery waiter cannot cancel the shielded service.
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)
        return StreamingResponse(deliver(), media_type="text/event-stream",
            headers={"Cache-Control": "no-store", "Vary": "Accept", "X-Accel-Buffering": "no"})


async def routed_response(service, body, key, accept, *, project_result=None, uncommitted_visible=None):
    """Negotiate after owned routing, before any routed part executes."""
    negotiate_turn_response(accept, allow_stream=True)
    loop = asyncio.get_running_loop()
    plan, choice = loop.create_future(), loop.create_future()
    delivery = SSEDelivery(service.records, project_result=project_result, uncommitted_visible=uncommitted_visible)
    async def prepared(intent):
        plan.set_result(intent)
        return await choice
    waiter = create_scoped_task(service.run(body, key, on_started=delivery.started,
        on_delta=delivery.delta, on_plan=prepared, on_frame=delivery.frame))
    try:
        await asyncio.wait({plan, waiter}, return_when=asyncio.FIRST_COMPLETED)
        if plan.done():
            representation = negotiate_turn_response(accept, allow_stream=plan.result() in {'ask', 'multi'})
            choice.set_result(representation)
            result = None
        else:
            result = await waiter
            representation = negotiate_turn_response(accept, allow_stream=result['turn']['intent'] in {'ask', 'multi'})
        if representation == 'sse':
            return delivery.response(lambda *_: asyncio.shield(waiter))
        result = result or await asyncio.shield(waiter)
        if project_result is not None:
            result = project_result(result)
        return JSONResponse(result, headers={'Vary':'Accept', 'Cache-Control':'no-store'})
    except BaseException as error:
        if not choice.done():
            if isinstance(error, HTTPException):
                choice.set_exception(error)
            else:
                # A disconnected client detaches delivery, not accepted work.
                choice.set_result('json')
        waiter.cancel()
        raise
