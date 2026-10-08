import { act, cleanup, fireEvent, render, screen } from '@testing-library/react';
import { afterEach, beforeEach, expect, it, vi } from 'vitest';
import SettingsPage from '@src/features/settings/SettingsPage';
import { forgetDeviceCredential, saveDeviceCredential, selectUserSpace } from '@src/shared/api/deviceTransport';

const settle = async () => act(async () => {});
const choice = { available: true, enabled: false, revision: 4, generation_revision: 3, mode_revision: 7 };
const response = (value, status = 200) => ({ ok: status === 200, status, json: async () => value });
function settings(mode = 'api') {
  return { load: vi.fn(async () => ({ model: {
    generation: { model: 'synthetic', revision: 3, configured: true, allow_remote: true },
    generation_mode: { mode, revision: 7 },
  }, privacy: { private_projects: [] } })), projects: vi.fn(async () => ({ items: [] })),
  fastModels: vi.fn(async () => ({ items: [] })) };
}
async function openModels(service = settings()) {
  const view = render(<SettingsPage api={service}/>);
  await settle();
  fireEvent.click(screen.getByRole('button', { name: '生成' }));
  await settle();
  return view;
}
beforeEach(() => { forgetDeviceCredential(); vi.stubGlobal('fetch', vi.fn(async () => response(choice))); });
afterEach(() => { cleanup(); forgetDeviceCredential(); vi.unstubAllGlobals(); });

it('loads the actual optional capability and shows its default off choice', async () => {
  await openModels();
  expect(fetch).toHaveBeenCalledOnce();
  expect(fetch.mock.calls[0][0]).toBe('/api/v2/settings/provider-store');
  expect(screen.getByRole('switch', { name: '服务商暂存' })).toHaveAttribute('aria-checked', 'false');
  expect(screen.getByRole('switch', { name: '服务商暂存' })).toHaveAttribute('title', expect.stringContaining('请求与回答'));
});

it('saves the enabled choice with all three original CAS revisions', async () => {
  fetch.mockImplementation(async (_url, init = {}) => response(init.method === 'PUT' ? { ...choice, enabled: true, revision: 5 } : choice));
  await openModels();
  fireEvent.click(screen.getByRole('switch', { name: '服务商暂存' }));
  await settle();
  const writes = fetch.mock.calls.filter(([, init]) => init.method === 'PUT');
  expect(writes).toHaveLength(1);
  expect(writes[0][0]).toBe('/api/v2/settings/provider-store');
  expect(JSON.parse(writes[0][1].body)).toEqual({ enabled: true, expected_revision: 4,
    expected_generation_revision: 3, expected_mode_revision: 7 });
});

it('keeps a rejected update off and presents the established refresh action', async () => {
  fetch.mockImplementation(async (_url, init = {}) => init.method === 'PUT'
    ? response({ detail: 'provider_store_revision_conflict' }, 409) : response(choice));
  await openModels();
  fireEvent.click(screen.getByRole('switch', { name: '服务商暂存' }));
  await settle();
  expect(screen.getByRole('alert')).toHaveTextContent('设置已变化 · 刷新');
  expect(screen.getByRole('switch', { name: '服务商暂存' })).toHaveAttribute('aria-checked', 'false');
  expect(fetch.mock.calls.filter(([, init]) => init.method === 'PUT')).toHaveLength(1);
});

it('checks capability availability and hides the switch when unsupported', async () => {
  fetch.mockResolvedValue(response({ ...choice, available: false, enabled: false }));
  await openModels();
  expect(fetch).toHaveBeenCalledOnce();
  expect(screen.queryByRole('switch', { name: '服务商暂存' })).not.toBeInTheDocument();
});

it('does not offer provider storage for the subscription mode', async () => {
  await openModels(settings('subscription'));
  expect(screen.queryByRole('switch', { name: '服务商暂存' })).not.toBeInTheDocument();
  expect(fetch.mock.calls.some(([url]) => String(url).endsWith('/provider-store'))).toBe(false);
});

it('rejects a late response from the prior user space while retaining the new user choice', async () => {
  saveDeviceCredential({ key: 'a'.repeat(43), device: { device_id: 'synthetic-device', user_id: 'admin' } });
  selectUserSpace({ user_id: 'old-user' });
  let releaseOld;
  fetch.mockImplementation(async (_url, init) => new Headers(init.headers).get('X-Chriptmas-Target-User') === 'old-user'
    ? new Promise(resolve => { releaseOld = resolve; }) : response({ ...choice, enabled: true, revision: 9 }));
  await openModels();
  expect(fetch).toHaveBeenCalledOnce();
  await act(async () => { selectUserSpace({ user_id: 'new-user' }); });
  await settle();
  expect(screen.getByRole('switch', { name: '服务商暂存' })).toHaveAttribute('aria-checked', 'true');
  await act(async () => { releaseOld(response(choice)); });
  expect(screen.getByRole('switch', { name: '服务商暂存' })).toHaveAttribute('aria-checked', 'true');
  expect(screen.queryByRole('alert')).not.toBeInTheDocument();
  expect(fetch.mock.calls.map(([, init]) => new Headers(init.headers).get('X-Chriptmas-Target-User')))
    .toEqual(['old-user', 'new-user']);
});
