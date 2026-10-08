import { recognitionApi } from '@src/shared/api/recognitionApi';
import { subscriptionRequest } from '@src/shared/ui/subscriptionApi';
import { createCompanionApi } from '@src/features/companion/companionApi';
import { loadCloudAsrProviderSettings } from '@src/features/rebuild/rebuildSettingsApi';
import { afterEach, expect, it, vi } from 'vitest';
import { libraryApi } from '@src/features/library/libraryApi';
import { workbenchApi } from '@src/features/workbench/workbenchApi';
import { settingsRequest } from '@src/features/settings/settingsApi';
afterEach(() => vi.unstubAllGlobals());
const calls = [
 ['recognition', () => recognitionApi.loadDocument({ projectId: 'alpha', documentId: 'doc' })],
 ['subscription', () => subscriptionRequest()],
 ['companion', () => createCompanionApi().getStatus()],
 ['ASR', () => loadCloudAsrProviderSettings()],
 ['library', () => libraryApi.list('source', 'alpha')],
 ['workbench', () => workbenchApi.threads('alpha')],
 ['ask JSON', () => workbenchApi.create({ project_id: 'alpha', intent: 'ask', text: '问' })],
 ['settings', () => settingsRequest('settings')],
];
it.each(calls)('%s maps an empty 502 response to a stable Chinese error', async (_, call) => {
 const json = vi.fn(async () => { throw new SyntaxError('Unexpected end of JSON input'); });
 vi.stubGlobal('fetch', vi.fn(async () => ({ ok: false, status: 502, json })));
 await expect(call()).rejects.toMatchObject({ code: 'server_unavailable', message: '连接未完成 · 重试' });
 expect(json).not.toHaveBeenCalled();
});
it.each(calls)('%s maps malformed successful JSON without exposing parser text', async (_, call) => {
 vi.stubGlobal('fetch', vi.fn(async () => ({ ok: true, status: 200, json: async () => { throw new SyntaxError('Unexpected token <'); } })));
 await expect(call()).rejects.toMatchObject({ code: 'invalid_response', message: '读取未完成 · 重试' });
});
it('keeps existing business errors and successful JSON', async () => {
 vi.stubGlobal('fetch', vi.fn(async () => ({ ok: false, status: 409, json: async () => ({ detail: 'source_changed_retry' }) })));
 await expect(workbenchApi.threads('alpha')).rejects.toMatchObject({ code: 'source_changed_retry', message: '资料已变化 · 重新发送' });
 await expect(libraryApi.list('source', 'alpha')).rejects.toThrow('内容已变化 · 刷新');
 fetch.mockResolvedValue({ ok: true, status: 200, json: async () => ({ items: [] }) });
 await expect(libraryApi.list('source', 'alpha')).resolves.toEqual({ items: [] });
});

it('retains the ask idempotency key after an unavailable server', async () => {
 const body = { project_id: 'f5-retry', intent: 'ask', text: '重试问题' };
 vi.stubGlobal('fetch', vi.fn().mockResolvedValueOnce({ ok: false, status: 502 }).mockResolvedValueOnce({ ok: true, status: 200, json: async () => ({ turn: { id: 'turn' } }) }));
 await expect(workbenchApi.create(body)).rejects.toMatchObject({ code: 'server_unavailable' });
 await workbenchApi.create(body);
 expect(fetch.mock.calls[1][1].headers['Idempotency-Key']).toBe(fetch.mock.calls[0][1].headers['Idempotency-Key']);
});

it.each(calls)('%s maps non-JSON error responses without rendering server HTML', async (_, call) => {
 vi.stubGlobal('fetch', vi.fn(async () => ({ ok: false, status: 400, text: async () => '<html>Bad Request</html>', json: async () => { throw new SyntaxError('Unexpected token <'); } })));
 await expect(call()).rejects.toMatchObject({ code: 'invalid_response', message: '读取未完成 · 重试' });
});
