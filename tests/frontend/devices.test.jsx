import React from 'react';
import { act, cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react';
import { afterEach, beforeEach, expect, it, vi } from 'vitest';
import { DeviceGate } from '../../src/frontend/src/features/settings/DeviceGate';
import { SettingsDevices } from '../../src/frontend/src/features/settings/SettingsDevices';
import { ProductFileLink } from '../../src/frontend/src/shared/ui/ProductFileLink';
import { forgetDeviceCredential, readDeviceCredential, saveDeviceCredential } from '../../src/frontend/src/shared/api/deviceTransport';

const key = 'k'.repeat(43);
let wire;
const response = (value, status = 200) => ({ ok: status < 400, status, json: async () => value });
const device = { device_id: 'device-a', user_id: 'local-user', name: '手机', created_at: '2026-10-05T00:00:00Z',
  last_seen_at: '2026-10-05T00:00:00Z', revoked_at: null, revision: 1 };
const listed = () => ({ mode: 'server', device_id: device.device_id, items: [device] });
beforeEach(() => {
  localStorage.clear(); forgetDeviceCredential();
  history.replaceState(null, '', '/');
  wire = vi.fn().mockResolvedValue(response(listed()));
  vi.stubGlobal('fetch', wire);
});
afterEach(() => { cleanup(); vi.useRealTimers(); vi.unstubAllGlobals(); vi.restoreAllMocks(); history.replaceState(null, '', '/'); });

it('reads a fragment once, removes it before requests and exchanges the code in JSON', async () => {
  history.replaceState(null, '', '/pair#code=' + 'p'.repeat(43));
  wire.mockResolvedValueOnce(response({}, 401)).mockResolvedValueOnce(response({ key, device }, 201));
  render(<DeviceGate><p>工作台已打开</p></DeviceGate>);
  expect(location.hash).toBe('');
  await screen.findByRole('button', { name: '配对' });
  fireEvent.change(screen.getByLabelText('设备名称'), { target: { value: '手机' } });
  fireEvent.click(screen.getByRole('button', { name: '配对' }));
  await screen.findByText('工作台已打开');
  const exchange = wire.mock.calls.find(([url]) => String(url).endsWith('/exchange'));
  expect(JSON.parse(exchange[1].body)).toEqual({ code: 'p'.repeat(43), name: '手机' });
  expect(new Headers(exchange[1].headers).get('Authorization')).toBeNull();
  expect(readDeviceCredential().key).toBe(key);
  expect(screen.queryByDisplayValue(key)).toBeNull();
});

it('desktop is already paired and a revoked server request returns to pairing', async () => {
  wire.mockResolvedValue(response({ mode: 'desktop', device_id: 'desktop', items: [] }));
  render(<DeviceGate><p>工作台已打开</p></DeviceGate>);
  await screen.findByText('工作台已打开');
  await act(async () => { forgetDeviceCredential(); });
  await screen.findByRole('button', { name: '配对' });
});

it('shows device rows and uses the displayed fact revision to revoke itself', async () => {
  saveDeviceCredential({ key, device });
  wire.mockImplementation(async (url) => String(url).endsWith('/revoke') ? response({ ...device, revoked_at: 'now', revision: 2 }) : response(listed()));
  render(<SettingsDevices projectId="default"/>);
  await screen.findByText('手机');
  fireEvent.click(screen.getByRole('button', { name: '作废手机' }));
  await waitFor(() => expect(readDeviceCredential()).toBeNull());
  const revoke = wire.mock.calls.find(([url]) => String(url).endsWith('/revoke'));
  expect(JSON.parse(revoke[1].body)).toEqual({ expected_revision: 1 });
  expect(new Headers(revoke[1].headers).get('Authorization')).toBe('Bearer ' + key);
});

it('revokes another device using the actual flat response and retains the current device identity', async () => {
  saveDeviceCredential({ key, device });
  const other = { ...device, device_id: 'device-b', name: '平板', revision: 3 };
  wire.mockImplementation(async url => String(url).endsWith('/device-b/revoke')
    ? response({ ...other, revoked_at: '2026-10-05T00:01:00Z', revision: 4 })
    : response({ ...listed(), items: [device, other] }));
  render(<SettingsDevices projectId="default"/>);
  await screen.findByText('平板');
  fireEvent.click(screen.getByRole('button', { name: '作废平板' }));
  await screen.findByText('已作废');
  expect(screen.getByText('手机')).toBeInTheDocument();
  expect(screen.getByText('平板')).toBeInTheDocument();
  expect(screen.getByRole('button', { name: '作废手机' })).toBeEnabled();
  expect(screen.queryByRole('button', { name: '作废平板' })).toBeNull();
  expect(readDeviceCredential().device.device_id).toBe(device.device_id);
  expect(readDeviceCredential().key === key).toBe(true);
  const revoke = wire.mock.calls.find(([url]) => String(url).endsWith('/device-b/revoke'));
  expect(JSON.parse(revoke[1].body)).toEqual({ expected_revision: 3 });
  expect(new Headers(revoke[1].headers).get('Authorization') === 'Bearer ' + key).toBe(true);
});

it('makes an expired QR unusable and offers regeneration', async () => {
  wire.mockImplementation(async (url) => String(url).endsWith('/pair') ? response({
    qr: 'data:image/png;base64,c3ludGhldGlj', url: location.origin + '/pair#code=transient', expires_at: new Date(Date.now() - 1000).toISOString()
  }) : response(listed()));
  render(<SettingsDevices projectId="default"/>);
  await screen.findByText('手机');
  fireEvent.click(screen.getByRole('button', { name: '添加设备' }));
  await screen.findByText('已过期');
  expect(screen.queryByRole('link', { name: '配对链接' })).toBeNull();
  expect(screen.getByRole('img', { name: '设备配对二维码' }).closest('.settings-device-qr')).toHaveClass('is-expired');
  expect(screen.getByRole('button', { name: '重新生成' })).toBeEnabled();
});

it('a late pairing response for an old settings scope cannot display credentials', async () => {
  let finish;
  wire.mockImplementation((url) => String(url).endsWith('/pair') ? new Promise(resolve => { finish = resolve; }) : Promise.resolve(response(listed())));
  const view = render(<SettingsDevices projectId="first"/>);
  await screen.findByText('手机');
  fireEvent.click(screen.getByRole('button', { name: '添加设备' }));
  view.rerender(<SettingsDevices projectId="second"/>);
  await act(async () => finish(response({ qr: 'data:image/png;base64,c3ludGhldGlj', url: '/pair#code=old', expires_at: new Date(Date.now() + 600000).toISOString() })));
  expect(screen.queryByRole('img', { name: '设备配对二维码' })).toBeNull();
  expect(screen.queryByRole('link', { name: '配对链接' })).toBeNull();
});

it('downloads product bytes with Bearer while external links retain their native target', async () => {
  saveDeviceCredential({ key, device });
  const original = new Blob(['original-image-bytes']);
  wire.mockResolvedValue({ ok: true, status: 200, blob: async () => original });
  const create = vi.fn().mockReturnValue('blob:synthetic');
  vi.stubGlobal('URL', class extends URL { static createObjectURL = create; static revokeObjectURL = vi.fn(); });
  const clicks = vi.spyOn(HTMLAnchorElement.prototype, 'click').mockImplementation(() => {});
  render(<><ProductFileLink href="/api/v2/image/original" name="图一.png">图一</ProductFileLink><ProductFileLink href="https://outside.example/file">外链</ProductFileLink></>);
  fireEvent.click(screen.getByRole('link', { name: '图一' }));
  await waitFor(() => expect(clicks).toHaveBeenCalledTimes(1));
  expect(create).toHaveBeenCalledWith(original);
  expect(new Headers(wire.mock.calls[0][1].headers).get('Authorization')).toBe('Bearer ' + key);
  expect(screen.getByRole('link', { name: '外链' }).getAttribute('href')).toBe('https://outside.example/file');
});

it('a late old item download cannot trigger a link in the newly selected item', async () => {
  saveDeviceCredential({ key, device }); let finish;
  wire.mockReturnValue(new Promise(resolve => { finish = resolve; }));
  const clicks = vi.spyOn(HTMLAnchorElement.prototype, 'click').mockImplementation(() => {});
  const view = render(<ProductFileLink href="/api/v2/image/first" scopeKey="first">原件</ProductFileLink>);
  fireEvent.click(screen.getByRole('link', { name: '原件' }));
  view.rerender(<ProductFileLink href="/api/v2/image/second" scopeKey="second">原件</ProductFileLink>);
  await act(async () => finish({ ok: true, status: 200, blob: async () => new Blob(['first']) }));
  expect(clicks).not.toHaveBeenCalled();
});

it('counts down the actual expiring code then dims its QR and disables its link', async () => {
  vi.useFakeTimers({ toFake: ['Date', 'setInterval', 'clearInterval'] });
  vi.setSystemTime(new Date('2026-10-05T00:00:00Z'));
  wire.mockImplementation(async url => String(url).endsWith('/pair') ? response({
    qr: 'data:image/png;base64,c3ludGhldGlj', url: '/pair#code=transient', expires_at: new Date(Date.now() + 2000).toISOString()
  }) : response(listed()));
  render(<SettingsDevices projectId="default"/>);
  await act(async () => {});
  fireEvent.click(screen.getByRole('button', { name: '添加设备' }));
  await act(async () => {});
  expect(screen.getByRole('timer')).toHaveTextContent('0:02');
  expect(screen.getByRole('link', { name: '配对链接' })).toBeInTheDocument();
  act(() => vi.advanceTimersByTime(1000));
  expect(screen.getByRole('timer')).toHaveTextContent('0:01');
  act(() => vi.advanceTimersByTime(1000));
  expect(screen.getByText('已过期')).toBeInTheDocument();
  expect(screen.getByRole('img', { name: '设备配对二维码' }).closest('.settings-device-qr')).toHaveClass('is-expired');
  expect(screen.queryByRole('link', { name: '配对链接' })).toBeNull();
  expect(screen.getByRole('button', { name: '重新生成' })).toBeEnabled();
});
