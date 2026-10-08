import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

let transport, wire;
const key = () => 'k'.repeat(43);
beforeEach(async () => {
  vi.resetModules(); localStorage.clear();
  wire = vi.fn().mockResolvedValue({ status: 200, ok: true });
  vi.stubGlobal('fetch', wire);
  transport = await import('../../../src/frontend/src/shared/api/deviceTransport');
});
afterEach(() => { vi.unstubAllGlobals(); vi.restoreAllMocks(); });

function paired(value = key()) {
  transport.saveDeviceCredential({ key: value, device: { device_id: 'device-a', user_id: 'local-user' } });
}

it('adds the current key only to same product origin and API paths', async () => {
  paired();
  await transport.productFetch('/api/v2/projects', { headers: { Accept: 'application/json' } });
  const options = wire.mock.calls[0][1];
  expect(new Headers(options.headers).get('Authorization')).toBe('Bearer ' + key());
  expect(new Headers(options.headers).get('Accept')).toBe('application/json');
  expect(options.redirect).toBe('error');
  for (const url of ['https://outside.example/api/v2/projects', '/assets/client.js', '/api2/private', '/api/health']) {
    await transport.productFetch(url);
    expect(new Headers(wire.mock.calls.at(-1)[1]?.headers).get('Authorization')).toBeNull();
  }
});

it('preserves multipart, stream headers, signal and idempotency without global fetch replacement', async () => {
  paired();
  const body = new FormData(), controller = new AbortController();
  body.append('file', new Blob(['original']), 'original.txt');
  await transport.productFetch('/api/v2/workbench/files', { method: 'POST', body, signal: controller.signal,
    headers: { Accept: 'text/event-stream', 'Idempotency-Key': 'request-a' } });
  const options = wire.mock.calls[0][1];
  expect(options.body).toBe(body); expect(options.signal).toBe(controller.signal);
  expect(new Headers(options.headers).get('Idempotency-Key')).toBe('request-a');
  expect(new Headers(options.headers).get('Content-Type')).toBeNull();
  expect(globalThis.fetch).toBe(wire);
});

it('exchanges a pairing code in a JSON body without sending an old key', async () => {
  paired();
  await transport.productFetch('/api/v2/devices/exchange', { method: 'POST', body: '{}' });
  expect(new Headers(wire.mock.calls[0][1]?.headers).get('Authorization')).toBeNull();
  expect(wire.mock.calls[0][1].redirect).toBe('error');
});

it('does not expose server keys to the Electron desktop backend', async () => {
  paired(); vi.stubGlobal('electronAPI', { backendBaseUrl: 'http://127.0.0.1:54321' });
  await transport.productFetch('http://127.0.0.1:54321/api/v2/projects');
  expect(new Headers(wire.mock.calls[0][1]?.headers).get('Authorization')).toBeNull();
});

it('a late old-key 401 cannot erase a newly paired credential', async () => {
  paired(); let finish;
  wire.mockReturnValueOnce(new Promise(resolve => { finish = resolve; }));
  const pending = transport.productFetch('/api/v2/projects');
  paired('n'.repeat(43));
  finish({ status: 401, ok: false }); await pending;
  await transport.productFetch('/api/v2/projects');
  expect(new Headers(wire.mock.calls[1][1].headers).get('Authorization')).toBe('Bearer ' + 'n'.repeat(43));
});

it('clears a revoked credential and announces pairing again', async () => {
  paired(); const changed = vi.fn(); window.addEventListener('chriptmas-device-required', changed);
  wire.mockResolvedValueOnce({ status: 401, ok: false });
  await transport.productFetch('/api/v2/projects');
  expect(changed).toHaveBeenCalledTimes(1);
  await transport.productFetch('/api/v2/projects');
  expect(new Headers(wire.mock.calls[1][1]?.headers).get('Authorization')).toBeNull();
  window.removeEventListener('chriptmas-device-required', changed);
});

it('uses the newly paired session key when Storage writes fail and the older slot remains readable', async () => {
  paired();
  const originalSlot = localStorage.getItem('chriptmas-server-device');
  const failedWrite = vi.spyOn(localStorage, 'setItem').mockImplementation(() => { throw new Error('storage_unavailable'); });
  const next = 'n'.repeat(43);
  paired(next);
  expect(failedWrite).toHaveBeenCalledTimes(1);
  expect(localStorage.getItem('chriptmas-server-device') === originalSlot).toBe(true);
  expect(transport.readDeviceCredential().key === next).toBe(true);
  await transport.productFetch('/api/v2/projects');
  expect(new Headers(wire.mock.calls[0][1].headers).get('Authorization') === 'Bearer ' + next).toBe(true);
});

it('keeps the session credential cleared when Storage deletion fails and the older slot remains readable', async () => {
  paired();
  const originalSlot = localStorage.getItem('chriptmas-server-device');
  const failedDelete = vi.spyOn(localStorage, 'removeItem').mockImplementation(() => { throw new Error('storage_unavailable'); });
  transport.forgetDeviceCredential();
  expect(failedDelete).toHaveBeenCalledTimes(1);
  expect(localStorage.getItem('chriptmas-server-device') === originalSlot).toBe(true);
  expect(transport.readDeviceCredential()).toBeNull();
  await transport.productFetch('/api/v2/projects');
  expect(new Headers(wire.mock.calls[0][1]?.headers).get('Authorization')).toBeNull();
});

it('loads the origin-bound persistent credential on a fresh module session', async () => {
  paired();
  vi.resetModules();
  transport = await import('../../../src/frontend/src/shared/api/deviceTransport');
  expect(transport.readDeviceCredential().key === key()).toBe(true);
  await transport.productFetch('/api/v2/projects');
  expect(new Headers(wire.mock.calls[0][1].headers).get('Authorization') === 'Bearer ' + key()).toBe(true);
});
