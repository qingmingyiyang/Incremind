import { afterEach, expect, it, vi } from 'vitest';
import { libraryApi } from '@src/features/library/libraryApi';
afterEach(() => { vi.unstubAllGlobals(); });
function server(value = { items: [] }, status = 200) {
  vi.stubGlobal('fetch', vi.fn(async () => ({ ok: status < 400, status, json: async () => value,
    headers: new Headers({ 'Content-Type': 'application/zip' }), blob: async () => new Blob(['PK'], { type: 'application/zip' }) })));
}
it('uses exact project and scene paths with the original desktop resolver', async () => {
  server(); vi.stubGlobal('electronAPI', { backendBaseUrl: 'http://127.0.0.1:9231/' });
  await libraryApi.skillExportMethods('project/one', { scene: '读 #书' });
  const url = new URL(fetch.mock.calls[0][0]);
  expect(url.origin).toBe('http://127.0.0.1:9231');
  expect(url.pathname).toBe('/api/v2/projects/project%2Fone/skill-exports/methods');
  expect(url.searchParams.get('scene')).toBe('读 #书');
});
it('posts only the frozen download CAS and returns a real blob', async () => {
  server(); const blob = await libraryApi.downloadSkillExport('alpha', 'skill-one', 8);
  expect(blob).toBeInstanceOf(Blob);
  expect(fetch.mock.calls[0][1].method).toBe('POST');
  expect(JSON.parse(fetch.mock.calls[0][1].body)).toEqual({ expected_revision: 8 });
});
it('keeps source numbering and explicit review CAS in JSON bodies', async () => {
  server(); const document = { steps: [{ text: '检查', sources: [2, 1] }] };
  await libraryApi.saveSkillExport('alpha', 'skill-one', 8, document);
  expect(fetch.mock.calls[0][1].method).toBe('PATCH');
  expect(JSON.parse(fetch.mock.calls[0][1].body)).toEqual({ expected_revision: 8, document });
  await libraryApi.reviewSkillExport('alpha', 'skill-one', 9);
  expect(JSON.parse(fetch.mock.calls[1][1].body)).toEqual({ expected_revision: 9 });
});
it('reports a stale binary export without parsing or downloading an error as ZIP', async () => {
  server({ detail: 'skill_sources_changed' }, 409);
  await expect(libraryApi.downloadSkillExport('alpha', 'skill-one', 8)).rejects.toMatchObject({ status: 409 });
});
it('posts directory output only with both frozen CAS revisions and explicit first confirmation', async () => {
  server(); const body = { expected_revision: 8, directory: 'D:\\temporary-skills', confirm_first_export: true, expected_confirmation_revision: 0 };
  await libraryApi.folderSkillExport('alpha', 'skill-one', body);
  expect(fetch.mock.calls[0][0]).toBe('/api/v2/projects/alpha/skill-exports/skill-one/folder');
  expect(fetch.mock.calls[0][1].method).toBe('POST');
  expect(JSON.parse(fetch.mock.calls[0][1].body)).toEqual(body);
});
