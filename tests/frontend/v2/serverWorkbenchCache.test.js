import { afterEach, beforeEach, expect, it, vi } from 'vitest';

let transport, api, wire;
const body = { project_id: 'default', intent: 'ask', text: '相同问题' };

beforeEach(async () => {
  vi.resetModules();
  localStorage.clear(); sessionStorage.clear();
  wire = vi.fn(); vi.stubGlobal('fetch', wire);
  transport = await import('../../../src/frontend/src/shared/api/deviceTransport');
  transport.saveDeviceCredential({ key: 'u'.repeat(43), device: { device_id: 'admin-device', user_id: 'local-user' } });
  ({ workbenchApi: api } = await import('../../../src/frontend/src/features/workbench/workbenchApi'));
});
afterEach(() => { vi.unstubAllGlobals(); vi.restoreAllMocks(); });

const submittedKey = index => new Headers(wire.mock.calls[index][1].headers).get('Idempotency-Key');

it('keeps the actual submission body and retry identity in each selected user space', async () => {
  wire.mockRejectedValue(new TypeError('synthetic network loss'));
  transport.selectUserSpace({ user_id: 'user-a', name: '甲' });
  await expect(api.create(body)).rejects.toMatchObject({ code: 'stream_disconnected' });
  const keyA = submittedKey(0);
  const slotA = transport.userStorageKey('chriptmas-v2-request:default');
  transport.selectUserSpace({ user_id: 'user-b', name: '乙' });
  await expect(api.create(body)).rejects.toMatchObject({ code: 'stream_disconnected' });
  const keyB = submittedKey(1);
  const slotB = transport.userStorageKey('chriptmas-v2-request:default');
  expect(keyA).not.toBe(keyB);
  expect(slotA).not.toBe(slotB);
  expect(JSON.parse(sessionStorage.getItem(slotA))).toEqual({ body: JSON.stringify(body), id: keyA });
  expect(JSON.parse(sessionStorage.getItem(slotB))).toEqual({ body: JSON.stringify(body), id: keyB });
  expect(sessionStorage.getItem('chriptmas-v2-request:default')).toBeNull();
  transport.selectUserSpace({ user_id: 'user-a', name: '甲' });
  await expect(api.create(body)).rejects.toMatchObject({ code: 'stream_disconnected' });
  expect(submittedKey(2)).toBe(keyA);
});

it('settles a former user response without clearing the current user retry slot', async () => {
  let finishA, jsonEntered;
  const entered = new Promise(resolve => { jsonEntered = resolve; });
  wire.mockResolvedValueOnce({ ok: true, status: 200, headers: new Headers({ 'Content-Type': 'application/json' }),
    json: () => { jsonEntered(); return new Promise(resolve => { finishA = resolve; }); } });
  wire.mockRejectedValue(new TypeError('synthetic network loss'));
  transport.selectUserSpace({ user_id: 'user-a', name: '甲' });
  const pendingA = api.create(body);
  await entered;
  transport.selectUserSpace({ user_id: 'user-b', name: '乙' });
  const bodyB = { ...body, text: '乙的问题' };
  await expect(api.create(bodyB)).rejects.toMatchObject({ code: 'stream_disconnected' });
  const keyB = submittedKey(1);
  finishA({ thread_id: 'thread-a', turn: { id: 'turn-a' } });
  await pendingA;
  await expect(api.create(bodyB)).rejects.toMatchObject({ code: 'stream_disconnected' });
  expect(submittedKey(2)).toBe(keyB);
  expect(JSON.parse(sessionStorage.getItem(transport.userStorageKey('chriptmas-v2-request:default'))))
    .toEqual({ body: JSON.stringify(bodyB), id: keyB });
});
