import { afterEach, beforeEach, expect, it, vi } from 'vitest';
import { saveDeviceCredential, forgetDeviceCredential } from '../../src/frontend/src/shared/api/deviceTransport';
import { workbenchApi } from '../../src/frontend/src/features/workbench/workbenchApi';
import { libraryApi } from '../../src/frontend/src/features/library/libraryApi';
import { settingsApi } from '../../src/frontend/src/features/settings/settingsApi';
import { recognitionApi } from '../../src/frontend/src/shared/api/recognitionApi';
import { subscriptionRequest } from '../../src/frontend/src/shared/ui/subscriptionApi';

let wire;
const credential = 'k'.repeat(43);
beforeEach(() => {
  forgetDeviceCredential();
  saveDeviceCredential({ key: credential, device: { device_id: 'device-a', user_id: 'local-user' } });
  wire = vi.fn().mockResolvedValue({ ok: true, status: 200, json: async () => ({ items: [], text: 'source', images: [] }), headers: new Headers() });
  vi.stubGlobal('fetch', wire);
});
afterEach(() => { vi.unstubAllGlobals(); forgetDeviceCredential(); });

it('actual product consumers add a device key through their preserved default transports', async () => {
  await settingsApi.load();
  await libraryApi.sourceText('project-a', 'source-a');
  await recognitionApi.loadDocument({ projectId: 'project-a', documentId: 'document-a' });
  await subscriptionRequest();
  expect(wire).toHaveBeenCalledTimes(4);
  for (const [, options] of wire.mock.calls) {
    expect(new Headers(options.headers).get('Authorization')).toBe('Bearer ' + credential);
    expect(options.redirect).toBe('error');
  }
});

it('actual multi-image upload and asking preserve multipart, stream signal and request identity', async () => {
  const files = [new File(['image-one'], 'one.png', { type: 'image/png' }), new File(['image-two'], 'two.png', { type: 'image/png' })];
  await workbenchApi.file('project-a', files[0], files.slice(1));
  const upload = wire.mock.calls[0][1];
  expect(upload.body.get('file')).toEqual(files[0]);
  expect(upload.body.getAll('files')).toEqual([files[1]]);
  expect(new Headers(upload.headers).get('Content-Type')).toBeNull();
  expect(new Headers(upload.headers).get('Authorization')).toBe('Bearer ' + credential);
  const controller = new AbortController();
  await workbenchApi.create({ project_id: 'project-a', intent: 'ask', text: 'read' }, { signal: controller.signal });
  const stream = wire.mock.calls[1][1];
  expect(stream.signal).toBe(controller.signal);
  expect(new Headers(stream.headers).get('Authorization')).toBe('Bearer ' + credential);
  expect(new Headers(stream.headers).get('Accept')).toContain('text/event-stream');
  expect(new Headers(stream.headers).get('Idempotency-Key')).toBeTruthy();
});
