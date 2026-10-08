import { afterEach, beforeEach, expect, it, vi } from 'vitest';
import { workbenchApi } from '@src/features/workbench/workbenchApi';

const turn = { id: 'continued-turn', intent: 'ask', receipt: { ask: { answer: '完成。' } } };
beforeEach(() => { sessionStorage.clear(); vi.stubGlobal('fetch', vi.fn()); });
afterEach(() => { vi.unstubAllGlobals(); });
it('posts once per explicit action and retains its key after uncertain delivery', async () => {
  fetch.mockRejectedValueOnce(new TypeError('synthetic network loss'))
    .mockResolvedValueOnce({ ok: true, json: async () => turn });
  await expect(workbenchApi.continue('continue-network', turn.id)).rejects.toMatchObject({ code: 'stream_disconnected' });
  expect(fetch).toHaveBeenCalledOnce();
  expect(await workbenchApi.continue('continue-network', turn.id)).toEqual(turn);
  expect(fetch).toHaveBeenCalledTimes(2);
  const calls = fetch.mock.calls;
  expect(calls.every(([, options]) => options.method === 'POST')).toBe(true);
  expect(new Headers(calls[0][1].headers).get('Idempotency-Key'))
    .toBe(new Headers(calls[1][1].headers).get('Idempotency-Key'));
  expect(JSON.parse(calls[0][1].body)).toEqual({ project_id: 'continue-network' });
});
it('rejects a different returned turn and preserves the uncertain action key', async () => {
  fetch.mockResolvedValueOnce({ ok: true, json: async () => ({ ...turn, id: 'another-turn' }) })
    .mockResolvedValueOnce({ ok: true, json: async () => turn });
  await expect(workbenchApi.continue('continue-identity', turn.id)).rejects.toMatchObject({ code: 'invalid_response' });
  expect(await workbenchApi.continue('continue-identity', turn.id)).toEqual(turn);
  expect(new Headers(fetch.mock.calls[0][1].headers).get('Idempotency-Key'))
    .toBe(new Headers(fetch.mock.calls[1][1].headers).get('Idempotency-Key'));
});
it('does not send a continuation for an already aborted scope', async () => {
  const controller = new AbortController(); controller.abort();
  await expect(workbenchApi.continue('continue-abort', turn.id, { signal: controller.signal }))
    .rejects.toMatchObject({ name: 'AbortError' });
  expect(fetch).not.toHaveBeenCalled();
});
