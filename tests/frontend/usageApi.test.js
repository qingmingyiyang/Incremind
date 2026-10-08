import { afterEach, expect, it, vi } from 'vitest';
import { libraryApi } from '@src/features/library/libraryApi';

afterEach(() => vi.unstubAllGlobals());

it('records a scoped document open without reading the empty 204 body', async () => {
  const json = vi.fn(() => { throw new Error('204 has no JSON'); });
  const fetch = vi.fn(async () => ({ status: 204, json }));
  vi.stubGlobal('fetch', fetch);
  await libraryApi.opened('alpha', 'summary', 'doc-one');
  expect(String(fetch.mock.calls[0][0])).toContain('/api/v2/usage/open');
  expect(JSON.parse(fetch.mock.calls[0][1].body)).toEqual({ project_id: 'alpha', kind: 'summary', id: 'doc-one' });
  expect(json).not.toHaveBeenCalled();
});

it('does not record original sources and isolates network failures from opening', async () => {
  const fetch = vi.fn(async () => { throw new Error('offline'); });
  vi.stubGlobal('fetch', fetch);
  await libraryApi.opened('alpha', 'source', 'source-one');
  expect(fetch).not.toHaveBeenCalled();
  await expect(libraryApi.opened('alpha', 'insight', 'insight-one')).resolves.toBeUndefined();
});
