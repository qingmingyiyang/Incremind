import { afterEach, expect, it, vi } from 'vitest';
import { libraryApi } from '@src/features/library/libraryApi';
import { workbenchApi } from '@src/features/workbench/workbenchApi';

const response = value => ({ ok: true, status: 200, json: async () => value });
afterEach(() => vi.unstubAllGlobals());

it('reads only qualified outcome choices through the scoped getter with cancellation', async () => {
  const value = { items: [{ document_id: 'new', title: '合成成果', version: 3 }] }, controller = new AbortController();
  vi.stubGlobal('fetch', vi.fn(async () => response(value)));
  expect(await libraryApi.outcomeChoices('甲 / 项目', { signal: controller.signal })).toEqual(value);
  const [url, options] = fetch.mock.calls[0];
  expect(new URL(String(url), 'http://localhost').pathname).toBe('/api/v2/library/outcomes');
  expect(new URL(String(url), 'http://localhost').searchParams.get('project_id')).toBe('甲 / 项目');
  expect(options.signal).toBe(controller.signal);
});

it('reads outcome versions through the original scoped API and forwards cancellation', async () => {
  const value = { items: [] }, controller = new AbortController();
  vi.stubGlobal('fetch', vi.fn(async () => response(value)));
  expect(await libraryApi.outcomeVersions('甲 / 项目', '稿/2', { signal: controller.signal })).toEqual(value);
  const [url, options] = fetch.mock.calls[0];
  expect(String(url)).toContain('/api/v2/library/outcomes/%E7%A8%BF%2F2/versions?');
  expect(new URL(String(url), 'http://localhost').searchParams.get('project_id')).toBe('甲 / 项目');
  expect(options.signal).toBe(controller.signal);
});

it.each([['auto', {}, undefined], ['explicit', { continue_from: 'old' }, 'old'], ['new', { continue_from: null }, null]])(
  'preserves redo %s choice and original CAS', async (_, options, expected) => {
    vi.stubGlobal('fetch', vi.fn(async () => response({ turn: {} })));
    await workbenchApi.redo('alpha', 'turn/1', 7, options);
    const body = JSON.parse(fetch.mock.calls[0][1].body);
    expect(body).toEqual({ project_id: 'alpha', expected_revision: 7, ...(expected !== undefined ? { continue_from: expected } : {}) });
  },
);
