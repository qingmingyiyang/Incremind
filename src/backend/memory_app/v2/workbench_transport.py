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


def read_stream_response(snapshot, after, *, sleep=asyncio.sleep, clock=monotonic):
    """A read-only subscriber; snapshot must never execute or recover a Turn."""
    initial = snapshot(after)
    async def deliver():
        cursor, state, last_heartbeat = after, initial, clock()
        while True:
            values, finished = state
            for sequence, event, value in values:
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
    def __init__(self, records=None):
        self.loop = asyncio.get_running_loop()
        self.queue = asyncio.Queue(maxsize=4)
        self.lock = Lock()
        self.pending = {}
        self.scheduled = False
        self.active = True
        self.records, self.pending_sequence = records, None

    def started(self, value):
        self._publish("started", value)

    def delta(self, text, *, sequence=None):
        with self.lock:
            if not self.active:
                return
            part = text.get('part') if isinstance(text, dict) else None
            value = text['text'] if isinstance(text, dict) else text
            self.pending[part] = self.pending.get(part, '') + value
            if part is None:
                self.pending_sequence = sequence
            if self.scheduled:
                return
            self.scheduled = True
        self._publish("delta", None)

    def frame(self, sequence, event, value):
        if event == 'delta':
            self.delta(value['text'], sequence=sequence)
        else:
            self._publish(event, value, sequence=sequence)

    def _publish(self, event, value, *, sequence=None):
        def put():
            if self.active:
                self.queue.put_nowait((event, value, sequence))
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
                            await self.queue.put(('reset', {'text': result['turn']['receipt']['ask']['partial']}, event['sequence']))
                        elif (event['kind'] == 'status' and event['state'] in {'completed', 'interrupted'}
                                and event['sequence'] > terminal['terminal'] - 3):
                            await self.queue.put(('status', stream_status(event), event['sequence']))
                await self.queue.put(('done', result, terminal['terminal'] if terminal is not None else None))
            except Exception as error:
                self._publish("error", {"code": safe_failure(error)})
        task = create_scoped_task(run())
        async def deliver():
            try:
                while True:
                    try:
                        event, value, sequence = await asyncio.wait_for(self.queue.get(), 15)
                    except TimeoutError:
                        yield ": keep-alive\n\n"
                        continue
                    if event == "delta":
                        with self.lock:
                            value, self.pending, self.scheduled = self.pending, {}, False
                            sequence, self.pending_sequence = self.pending_sequence, None
                        for part, text in value.items():
                            data = {'text':text, **({'part':part} if part is not None else {})}
                            yield sse_event('delta', data, sequence if part is None else None)
                        continue
                    yield sse_event(event, value, sequence)
                    if event in {"done", "error"}:
                        break
            finally:
                with self.lock:
                    self.active, self.pending = False, {}
                # Cancelling the delivery waiter cannot cancel the shielded service.
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)
        return StreamingResponse(deliver(), media_type="text/event-stream",
            headers={"Cache-Control": "no-store", "Vary": "Accept", "X-Accel-Buffering": "no"})


async def routed_response(service, body, key, accept):
    """Negotiate after owned routing, before any routed part executes."""
    negotiate_turn_response(accept, allow_stream=True)
    loop = asyncio.get_running_loop()
    plan, choice = loop.create_future(), loop.create_future()
    delivery = SSEDelivery(service.records)
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
