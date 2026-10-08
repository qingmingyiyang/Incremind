import { afterEach, beforeEach, expect, it, vi } from 'vitest';
import { workbenchApi } from '@src/features/workbench/workbenchApi';

const final = { thread_id: 'thread-1', turn: { id: 'turn-1', receipt: { ask: { answer: '中文😀' } } } };
const frame = (event, data) => `event: ${event}\r\ndata: ${JSON.stringify(data)}\r\n\r\n`;
function response(text, chunkSize = 3) {
  const bytes = new TextEncoder().encode(text);
  return { ok: true, headers: new Headers({ 'Content-Type': 'text/event-stream' }), body: new ReadableStream({ start(controller) {
    for (let offset = 0; offset < bytes.length; offset += chunkSize) controller.enqueue(bytes.slice(offset, offset + chunkSize));
    controller.close();
  } }) };
}
beforeEach(() => { sessionStorage.clear(); vi.stubGlobal('fetch', vi.fn()); });
afterEach(() => vi.unstubAllGlobals());

it('retains the request key when session storage rejects writes', async () => {
  vi.stubGlobal('sessionStorage', { getItem: () => null, setItem: () => { throw new Error('Unavailable'); }, removeItem: () => {} });
  fetch.mockResolvedValueOnce(response(frame('delta', { text: '未完成' }))).mockResolvedValueOnce(response(frame('done', final)));
  const body = { project_id: 'storage-blocked', text: '问题？', intent: 'ask' };
  await expect(workbenchApi.create(body)).rejects.toThrow();
  await workbenchApi.create(body);
  expect(fetch.mock.calls[1][1].headers['Idempotency-Key']).toBe(fetch.mock.calls[0][1].headers['Idempotency-Key']);
});

it('decodes split UTF-8 and CRLF SSE and delivers started delta and final once', async () => {
  fetch.mockResolvedValue(response(': keep-alive\r\n\r\n' + frame('started', { thread_id: 'thread-1', turn: { id: 'turn-1' } })
    + frame('delta', { text: '中文😀' }) + frame('done', final)));
  const onStarted = vi.fn(), onDelta = vi.fn();
  expect(await workbenchApi.create({ project_id: 'alpha', text: '问题？', intent: 'ask' }, { onStarted, onDelta })).toEqual(final);
  expect(onStarted).toHaveBeenCalledOnce(); expect(onDelta).toHaveBeenCalledExactlyOnceWith('中文😀');
  expect(fetch.mock.calls[0][1].headers.Accept).toContain('text/event-stream');
  expect(fetch.mock.calls[0][1].headers['Idempotency-Key']).toBeTruthy();
});

it('accepts negotiated JSON without a second POST', async () => {
  fetch.mockResolvedValue({ ok: true, headers: new Headers({ 'Content-Type': 'application/json' }), json: async () => final });
  expect(await workbenchApi.create({ project_id: 'alpha', text: '问题？', intent: 'ask' })).toEqual(final);
  expect(fetch).toHaveBeenCalledOnce();
});

it('retains an uncertain request key after disconnect and never automatically reposts', async () => {
  fetch.mockResolvedValueOnce(response(frame('delta', { text: '未完成' }))).mockResolvedValueOnce(response(frame('done', final)));
  const body = { project_id: 'alpha', text: '问题？', intent: 'ask' };
  await expect(workbenchApi.create(body)).rejects.toThrow();
  expect(fetch).toHaveBeenCalledOnce();
  await workbenchApi.create(body);
  expect(fetch.mock.calls[1][1].headers['Idempotency-Key']).toBe(fetch.mock.calls[0][1].headers['Idempotency-Key']);
});

it('keeps the key while the previous operation is in progress', async () => {
  fetch.mockResolvedValueOnce(response(frame('error', { code: 'turn_in_progress' }))).mockResolvedValueOnce(response(frame('done', final)));
  const body = { project_id: 'alpha', text: '问题？', intent: 'ask' };
  await expect(workbenchApi.create(body)).rejects.toMatchObject({ code: 'turn_in_progress' });
  await workbenchApi.create(body);
  expect(fetch.mock.calls[1][1].headers['Idempotency-Key']).toBe(fetch.mock.calls[0][1].headers['Idempotency-Key']);
});

it('uses a new key only after a terminal failure and an explicit retry', async () => {
  fetch.mockResolvedValueOnce(response(frame('error', { code: 'answer_generation_failed' }))).mockResolvedValueOnce(response(frame('done', final)));
  const body = { project_id: 'alpha', text: '问题？', intent: 'ask' };
  await expect(workbenchApi.create(body)).rejects.toMatchObject({ code: 'answer_generation_failed' });
  expect(fetch).toHaveBeenCalledOnce();
  await workbenchApi.create(body);
  expect(fetch.mock.calls[1][1].headers['Idempotency-Key']).not.toBe(fetch.mock.calls[0][1].headers['Idempotency-Key']);
});
