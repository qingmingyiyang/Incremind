import { afterEach, beforeEach, expect, it, vi } from 'vitest';

let transport, wire;
const key = 'u'.repeat(43);
beforeEach(async () => {
  vi.resetModules(); localStorage.clear();
  wire = vi.fn().mockResolvedValue({ status: 200, ok: true });
  vi.stubGlobal('fetch', wire);
  transport = await import('../../../src/frontend/src/shared/api/deviceTransport');
  transport.saveDeviceCredential({ key, device: { device_id: 'device-admin', user_id: 'local-user' } });
});
afterEach(() => { vi.unstubAllGlobals(); vi.restoreAllMocks(); });

it('selects target space separately from caller and includes it only in authenticated product calls', async () => {
  transport.selectUserSpace({ user_id: 'user-b', name: '乙' });
  expect(transport.readDeviceCredential().device.user_id).toBe('local-user');
  await transport.productFetch('/api/v2/workbench/files', { method: 'POST', body: new FormData() });
  const options = wire.mock.calls[0][1];
  expect(new Headers(options.headers).get('X-Chriptmas-Target-User')).toBe('user-b');
  expect(new Headers(options.headers).get('Authorization')).toBe('Bearer ' + key);
  expect(options.redirect).toBe('error');
  for (const url of ['/api/health', '/api/v2/devices/exchange', '/assets/a.js', 'https://outside.example/api/v2/projects']) {
    await transport.productFetch(url);
    expect(new Headers(wire.mock.calls.at(-1)[1]?.headers).get('X-Chriptmas-Target-User')).toBeNull();
  }
});

it('uses different browser state namespaces for caller and selected user and desktop keys stay identical', () => {
  const own = transport.userStorageKey('chriptmas-v2-thread:default');
  transport.selectUserSpace({ user_id: 'user-b', name: '乙' });
  const other = transport.userStorageKey('chriptmas-v2-thread:default');
  expect(other).not.toBe(own);
  expect(other.includes(key)).toBe(false);
  transport.selectUserSpace(null);
  expect(transport.userStorageKey('chriptmas-v2-thread:default')).toBe(own);
  vi.stubGlobal('electronAPI', { backendBaseUrl: 'http://127.0.0.1:12345' });
  expect(transport.userStorageKey('chriptmas-v2-thread:default')).toBe('chriptmas-v2-thread:default');
});

it('rejects a late response from a previous target without erasing the current credential', async () => {
  transport.selectUserSpace({ user_id: 'user-b', name: '乙' });
  let finish;
  wire.mockReturnValueOnce(new Promise(resolve => { finish = resolve; }));
  const pending = transport.productFetch('/api/v2/projects');
  transport.selectUserSpace(null);
  finish({ status: 200, ok: true });
  await expect(pending).rejects.toThrow('user_space_changed');
  expect(transport.readDeviceCredential().key === key).toBe(true);
  expect(transport.readUserSpace()).toBeNull();
});
