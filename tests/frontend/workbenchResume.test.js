import { afterEach, beforeEach, expect, it, vi } from 'vitest';
import { workbenchApi } from '@src/features/workbench/workbenchApi';

const final = { thread_id: 'thread-resume', turn: { id: 'turn-resume', receipt: { ask: { answer: '甲乙😀' } } } };
const started = { thread_id: final.thread_id, turn: { id: final.turn.id, intent: 'ask' } };
const frame = (id, event, data) => `${id === null ? '' : `id: ${id}\r\n`}event: ${event}\r\ndata: ${JSON.stringify(data)}\r\n\r\n`;
const response = text => ({ ok: true, status: 200, headers: new Headers({ 'Content-Type': 'text/event-stream' }),
  body: new ReadableStream({ start(controller) { controller.enqueue(new TextEncoder().encode(text)); controller.close(); } }) });
const body = { project_id: 'resume-alpha', text: '问题', intent: 'ask' };
beforeEach(() => { sessionStorage.clear(); vi.useFakeTimers(); vi.stubGlobal('fetch', vi.fn()); });
afterEach(() => { vi.useRealTimers(); vi.restoreAllMocks(); vi.unstubAllGlobals(); });

it('flushes accepted text before GET, acknowledges ids and drops replay duplicates without another POST', async () => {
  fetch.mockResolvedValueOnce(response(frame(1, 'started', started) + frame(2, 'delta', { text: '甲' })))
    .mockResolvedValueOnce(response(frame(2, 'delta', { text: '甲' }) + frame(3, 'delta', { text: '乙😀' }) + frame(5, 'done', final)));
  const texts = [], onStarted = vi.fn();
  const pending = workbenchApi.create(body, { onStarted, onDelta: text => texts.push(text) });
  pending.catch(() => {});
  await vi.advanceTimersByTimeAsync(1000);
  expect(await pending).toEqual(final);
  expect(texts.join('')).toBe('甲乙😀'); expect(onStarted).toHaveBeenCalledOnce();
  expect(fetch).toHaveBeenCalledTimes(2);
  const [url, options] = fetch.mock.calls[1];
  expect(url).toContain('/turns/turn-resume/stream?');
  expect(new URL(url, location.href).searchParams.get('after')).toBe('2');
  expect(new URL(url, location.href).searchParams.get('project_id')).toBe(body.project_id);
  expect(options.method).toBe('GET'); expect(options.headers['Last-Event-ID']).toBe('2');
  expect(options.body).toBeUndefined();
});

it('can refresh an existing turn with only GET and an explicit saved cursor', async () => {
  fetch.mockResolvedValue(response(frame(8, 'done', final)));
  expect(await workbenchApi.resume(body.project_id, final.turn.id, { after: 7 })).toEqual(final);
  expect(fetch).toHaveBeenCalledOnce();
  expect(fetch.mock.calls[0][1].method).toBe('GET');
  expect(new URL(fetch.mock.calls[0][0], location.href).searchParams.get('after')).toBe('7');
});

it('retains resets and status ordering while ignoring duplicate committed events', async () => {
  const reset = { text: '完整旧段\n\n' }, status = { state: 'completed' };
  fetch.mockResolvedValue(response(frame(4, 'reset', reset) + frame(4, 'reset', reset)
    + frame(5, 'delta', { text: '新段' }) + frame(6, 'status', status) + frame(7, 'done', final)));
  const received = [];
  await workbenchApi.resume(body.project_id, final.turn.id, { onReset: value => received.push(['reset', value]),
    onDelta: value => received.push(['delta', value]), onStatus: value => received.push(['status', value]) });
  expect(received).toEqual([['reset', reset], ['delta', '新段'], ['status', status]]);
});

it('backs off GET failures from one second to thirty seconds without reposting', async () => {
  fetch.mockRejectedValueOnce(new TypeError('synthetic network')).mockRejectedValueOnce(new TypeError('synthetic network'))
    .mockRejectedValueOnce(new TypeError('synthetic network')).mockRejectedValueOnce(new TypeError('synthetic network'))
    .mockRejectedValueOnce(new TypeError('synthetic network')).mockRejectedValueOnce(new TypeError('synthetic network'))
    .mockRejectedValueOnce(new TypeError('synthetic network')).mockResolvedValue(response(frame(9, 'done', final)));
  const pending = workbenchApi.resume(body.project_id, final.turn.id);
  await vi.advanceTimersByTimeAsync(0);
  for (const delay of [1000, 2000, 4000, 8000, 16000, 30000, 30000]) {
    const count = fetch.mock.calls.length;
    await vi.advanceTimersByTimeAsync(delay - 1); expect(fetch).toHaveBeenCalledTimes(count);
    await vi.advanceTimersByTimeAsync(1); expect(fetch).toHaveBeenCalledTimes(count + 1);
  }
  expect(await pending).toEqual(final);
  expect(fetch.mock.calls.every(([, options]) => options.method === 'GET')).toBe(true);
});

it('waits offline and reconnects immediately on online without waiting for the remaining delay', async () => {
  vi.spyOn(navigator, 'onLine', 'get').mockReturnValue(false);
  fetch.mockResolvedValue(response(frame(1, 'done', final)));
  const pending = workbenchApi.resume(body.project_id, final.turn.id);
  await vi.advanceTimersByTimeAsync(60000); expect(fetch).not.toHaveBeenCalled();
  vi.spyOn(navigator, 'onLine', 'get').mockReturnValue(true);
  window.dispatchEvent(new Event('online'));
  await vi.advanceTimersByTimeAsync(0);
  expect(await pending).toEqual(final); expect(fetch).toHaveBeenCalledOnce();
  vi.restoreAllMocks();
});

it('aborts backoff when leaving the thread and removes pending timers', async () => {
  fetch.mockRejectedValue(new TypeError('synthetic network'));
  const control = new AbortController();
  const pending = workbenchApi.resume(body.project_id, final.turn.id, { signal: control.signal });
  const rejected = expect(pending).rejects.toMatchObject({ name: 'AbortError' });
  await vi.advanceTimersByTimeAsync(0); expect(fetch).toHaveBeenCalledOnce();
  control.abort(); await rejected;
  await vi.advanceTimersByTimeAsync(60000); expect(fetch).toHaveBeenCalledOnce(); expect(vi.getTimerCount()).toBe(0);
});

it('stops on an actual authorization rejection without retrying reads or writes', async () => {
  fetch.mockResolvedValue({ ok: false, status: 401, json: async () => ({ detail: 'device_required' }) });
  await expect(workbenchApi.resume(body.project_id, final.turn.id)).rejects.toMatchObject({ code: 'device_required' });
  await vi.advanceTimersByTimeAsync(60000); expect(fetch).toHaveBeenCalledOnce();
});

it('negotiates Do SSE and resumes its accepted identity without another POST', async () => {
  const doStarted = { ...started, turn: { ...started.turn, intent: 'do' } };
  const doFinal = { ...final, turn: { ...final.turn, receipt: { do: { state: 'done' } } } };
  fetch.mockResolvedValueOnce(response(frame(null, 'started', doStarted)))
    .mockResolvedValueOnce(response(frame(2, 'done', doFinal)));
  const pending = workbenchApi.create({ ...body, intent: 'do' });
  pending.catch(() => {});
  await vi.advanceTimersByTimeAsync(1000); expect(await pending).toEqual(doFinal);
  expect(fetch.mock.calls.map(([, options]) => options.method)).toEqual(['POST', 'GET']);
  expect(fetch.mock.calls[0][1].headers.Accept).toContain('text/event-stream');
});

it('does not classify a display callback TypeError as a network retry', async () => {
  fetch.mockImplementation(async () => response(frame(2, 'status', { state: 'running' })));
  const control = new AbortController(), error = new TypeError('synthetic consumer failure');
  const pending = workbenchApi.resume(body.project_id, final.turn.id, { signal: control.signal, onStatus: () => { throw error; } });
  pending.catch(() => {});
  try {
    await vi.advanceTimersByTimeAsync(1000);
    expect(fetch).toHaveBeenCalledOnce();
    await expect(pending).rejects.toBe(error);
  } finally { control.abort(); await pending.catch(() => {}); }
});

it('acknowledges a delta only after the animation frame has actually displayed it', async () => {
  let controller;
  fetch.mockResolvedValue({ ok: true, status: 200, headers: new Headers({ 'Content-Type': 'text/event-stream' }),
    body: new ReadableStream({ start(value) { controller = value; value.enqueue(new TextEncoder().encode(frame(1, 'started', started) + frame(2, 'delta', { text: '甲' }))); } }) });
  const texts = [], cursors = [], pending = workbenchApi.create(body, { onDelta: value => texts.push(value), onCursor: value => cursors.push(value) });
  pending.catch(() => {});
  try {
    await vi.advanceTimersByTimeAsync(0); expect(cursors).toEqual([1]); expect(texts).toEqual([]);
    await vi.advanceTimersByTimeAsync(16); expect(texts).toEqual(['甲']); expect(cursors.at(-1)).toBe(2);
  } finally {
    controller.enqueue(new TextEncoder().encode(frame(4, 'done', final))); controller.close(); await pending;
  }
});

it('reports malformed JSON with a fixed code and does not expose response text', async () => {
  fetch.mockResolvedValue(response('id: 1\nevent: status\ndata: synthetic-secret-not-json\n\n'));
  await expect(workbenchApi.resume(body.project_id, final.turn.id)).rejects.toMatchObject({ code: 'invalid_response' });
  expect(fetch).toHaveBeenCalledOnce();
});

it('keeps reading an accepted headless Do until its persisted stream appears', async () => {
  const completed = { ...final, turn: { ...final.turn, intent: 'do', receipt: { do: { state: 'done' } } } };
  fetch.mockResolvedValueOnce(response(frame(null, 'started', { ...started, turn: { ...started.turn, intent: 'do' } })))
    .mockResolvedValueOnce({ ok: false, status: 404, json: async () => ({ detail: 'workbench_not_found' }) })
    .mockResolvedValueOnce(response(frame(5, 'done', completed)));
  const pending = workbenchApi.create({ ...body, intent: 'do' }); pending.catch(() => {});
  await vi.advanceTimersByTimeAsync(3000);
  expect(await pending).toEqual(completed);
  expect(fetch.mock.calls.map(([, options]) => options.method)).toEqual(['POST', 'GET', 'GET']);
  expect(fetch.mock.calls.slice(1).every(([url]) => new URL(url, location.href).searchParams.get('after') === '0')).toBe(true);
});

it('cancels an active reader on thread exit without delivering or reconnecting', async () => {
  const cancelled = vi.fn();
  fetch.mockResolvedValue({ ok: true, status: 200, headers: new Headers({ 'Content-Type': 'text/event-stream' }),
    body: new ReadableStream({ start(controller) { controller.enqueue(new TextEncoder().encode(frame(1, 'started', started))); }, cancel: cancelled }) });
  const control = new AbortController(), onDelta = vi.fn();
  const pending = workbenchApi.create(body, { signal: control.signal, onDelta }); pending.catch(() => {});
  await vi.advanceTimersByTimeAsync(0); control.abort();
  await expect(pending).rejects.toMatchObject({ name: 'AbortError' });
  await vi.advanceTimersByTimeAsync(60000);
  expect(cancelled).toHaveBeenCalledOnce(); expect(fetch).toHaveBeenCalledOnce(); expect(onDelta).not.toHaveBeenCalled();
});

it.each([
  ['raf', null, 'create'], ['terminal-eof', null, 'create'],
  ['raf', 'stream_disconnected', 'create'], ['terminal-eof', 'server_unavailable', 'resume'],
])('rejects a delta consumer error from %s (%s/%s) without acknowledging unseen text or GET', async (phase, code, method) => {
  const error = new TypeError('synthetic delta consumer failure'), cursors = [], timerErrors = [];
  if (code) error.code = code;
  let controller, reader;
  const stream = new ReadableStream({ start(value) {
    controller = value;
    value.enqueue(new TextEncoder().encode(frame(1, 'started', started) + frame(2, 'delta', { text: '甲' })
      + (phase === 'terminal-eof' ? frame(4, 'done', final) : '')));
    if (phase === 'terminal-eof') value.close();
  } });
  const nativeGetReader = stream.getReader.bind(stream);
  vi.spyOn(stream, 'getReader').mockImplementation(() => {
    reader = nativeGetReader();
    vi.spyOn(reader, 'cancel'); vi.spyOn(reader, 'releaseLock');
    return reader;
  });
  fetch.mockResolvedValueOnce({ ok: true, status: 200, headers: new Headers({ 'Content-Type': 'text/event-stream' }), body: stream })
    .mockResolvedValue(response(frame(5, 'done', final)));
  const options = {
    onDelta: () => { throw error; }, onCursor: value => cursors.push(value),
  };
  const pending = method === 'resume' ? workbenchApi.resume(`consumer-${phase}`, final.turn.id, options)
    : workbenchApi.create({ ...body, project_id: `consumer-${phase}` }, options);
  const outcome = pending.then(value => ({ value }), failure => ({ error: failure }));
  await vi.advanceTimersByTimeAsync(0);
  if (phase === 'raf') {
    await vi.advanceTimersByTimeAsync(16).catch(failure => timerErrors.push(failure));
    // Let a broken implementation leave its read loop without waiting forever;
    // a correct implementation has already cancelled this actual reader.
    if (!reader.cancel.mock.calls.length) controller.close();
  }
  await vi.advanceTimersByTimeAsync(1000);
  expect(fetch).toHaveBeenCalledOnce();
  const settled = await outcome;
  expect(settled).toEqual({ error }); expect(settled.error).toBe(error);
  expect(timerErrors).toEqual([]);
  expect(reader.cancel).toHaveBeenCalledOnce(); expect(reader.releaseLock).toHaveBeenCalledOnce();
  expect(fetch).toHaveBeenCalledOnce();
  expect(cursors.length).toBeGreaterThan(0); expect(cursors.every(value => value === 1)).toBe(true);
});

it.each(['started', 'status', 'cursor'])('does not treat a coded %s consumer failure as a reconnect', async kind => {
  const error = new TypeError('synthetic classified consumer failure'); error.code = 'stream_disconnected';
  const cursors = [], stream = response(frame(1, 'started', started)
    + frame(2, 'status', { state: 'running' }) + frame(3, 'done', final));
  let reader;
  const nativeGetReader = stream.body.getReader.bind(stream.body);
  vi.spyOn(stream.body, 'getReader').mockImplementation(() => {
    reader = nativeGetReader(); vi.spyOn(reader, 'cancel'); vi.spyOn(reader, 'releaseLock'); return reader;
  });
  fetch.mockResolvedValueOnce(stream).mockResolvedValue(response(frame(4, 'done', final)));
  const pending = workbenchApi.create({ ...body, project_id: `consumer-${kind}` }, {
    onStarted: () => { if (kind === 'started') throw error; },
    onStatus: () => { if (kind === 'status') throw error; },
    onCursor: value => { if (kind === 'cursor') throw error; cursors.push(value); },
  });
  const outcome = pending.then(value => ({ value }), failure => ({ error: failure }));
  await vi.advanceTimersByTimeAsync(1000);
  expect(fetch).toHaveBeenCalledOnce();
  expect((await outcome).error).toBe(error);
  expect(fetch).toHaveBeenCalledOnce();
  expect(reader.cancel).toHaveBeenCalledOnce(); expect(reader.releaseLock).toHaveBeenCalledOnce();
  expect(cursors).toEqual(kind === 'status' ? [1] : []);
});

it('retains the accepted request key when a consumer throws a terminal error code', async () => {
  const error = new TypeError('synthetic consumer terminal code'); error.code = 'answer_generation_failed';
  const requestBody = { ...body, project_id: 'consumer-terminal-key' };
  const stream = response(frame(1, 'started', started) + frame(3, 'done', final));
  let reader;
  const nativeGetReader = stream.body.getReader.bind(stream.body);
  vi.spyOn(stream.body, 'getReader').mockImplementation(() => {
    reader = nativeGetReader(); vi.spyOn(reader, 'cancel'); vi.spyOn(reader, 'releaseLock'); return reader;
  });
  fetch.mockResolvedValueOnce(stream).mockResolvedValueOnce(response(frame(3, 'done', final)));
  await expect(workbenchApi.create(requestBody, { onStarted: () => { throw error; } })).rejects.toBe(error);
  expect(fetch).toHaveBeenCalledOnce();
  expect(reader.cancel).toHaveBeenCalledOnce(); expect(reader.releaseLock).toHaveBeenCalledOnce();
  const originalKey = fetch.mock.calls[0][1].headers['Idempotency-Key'];
  expect(originalKey).toBeTruthy();
  expect(await workbenchApi.create(requestBody)).toEqual(final);
  expect(fetch.mock.calls.map(([, options]) => options.method)).toEqual(['POST', 'POST']);
  expect(fetch.mock.calls[1][1].headers['Idempotency-Key']).toBe(originalKey);
});
