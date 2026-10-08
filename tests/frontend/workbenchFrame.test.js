import { afterEach, beforeEach, expect, it, vi } from 'vitest';
import { workbenchApi } from '@src/features/workbench/workbenchApi';

const frame = (event, data) => new TextEncoder().encode(`event: ${event}\ndata: ${JSON.stringify(data)}\n\n`);
let controller, callbacks;
beforeEach(() => {
  sessionStorage.clear(); callbacks = new Map(); let next = 0;
  vi.stubGlobal('requestAnimationFrame', vi.fn(callback => { callbacks.set(++next, callback); return next; }));
  vi.stubGlobal('cancelAnimationFrame', vi.fn(id => callbacks.delete(id)));
  vi.stubGlobal('fetch', vi.fn(async () => ({ ok: true, headers: new Headers({ 'Content-Type': 'text/event-stream' }),
    body: new ReadableStream({ start(value) { controller = value; } }) })));
});
afterEach(() => vi.unstubAllGlobals());
const settle = async () => { for (let index = 0; index < 110; index++) await Promise.resolve(); };
function animate() { const pending = [...callbacks.values()]; callbacks.clear(); pending.forEach(callback => callback(16)); }
const body = { project_id: 'frame-test', text: '问题？', intent: 'ask' };

it('submits one ordered update for 100 deltas in one animation frame', async () => {
  const onDelta = vi.fn(), pending = workbenchApi.create(body, { onDelta }); await settle();
  for (let index = 0; index < 100; index++) controller.enqueue(frame('delta', { text: `${index},` }));
  await settle(); expect(onDelta).not.toHaveBeenCalled(); expect(callbacks.size).toBe(1);
  animate(); expect(onDelta).toHaveBeenCalledExactlyOnceWith(Array.from({ length: 100 }, (_, index) => `${index},`).join(''));
  controller.enqueue(frame('done', { thread_id: 't', turn: { id: 'r' } })); await pending;
  expect(callbacks.size).toBe(0);
});

it('flushes the final buffered prefix before delivering the validated receipt', async () => {
  const events = [], pending = workbenchApi.create(body, { onDelta: text => events.push(text) }).then(value => { events.push(value); return value; });
  await settle(); controller.enqueue(frame('delta', { text: '中文😀' }));
  const final = { thread_id: 't', turn: { id: 'r', receipt: { ask: { answer: '完整回答' } } } };
  controller.enqueue(frame('done', final)); expect(await pending).toEqual(final);
  expect(events).toEqual(['中文😀', final]); expect(callbacks.size).toBe(0);
});

it('drops buffered text and cancels its frame when the request is aborted', async () => {
  const abort = new AbortController(), onDelta = vi.fn();
  const pending = workbenchApi.create(body, { signal: abort.signal, onDelta });
  const failure = expect(pending).rejects.toThrow(); await settle();
  controller.enqueue(frame('delta', { text: '旧项目' })); await settle();
  abort.abort(); await failure; animate(); expect(onDelta).not.toHaveBeenCalled(); expect(callbacks.size).toBe(0);
});
