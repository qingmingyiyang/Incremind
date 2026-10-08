import React from 'react';
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react';
import { afterEach, beforeEach, expect, it, vi } from 'vitest';
import { SettingsUsers } from '../../src/frontend/src/features/settings/SettingsUsers';
import { SettingsDevices } from '../../src/frontend/src/features/settings/SettingsDevices';
import { readUserSpace, saveDeviceCredential, forgetDeviceCredential } from '../../src/frontend/src/shared/api/deviceTransport';

const admin = { user_id: 'local-user', name: '本机', role: 'admin' };
const other = { user_id: 'user-b', name: '乙', role: 'user', revision: 2, disabled_at: null, device_count: 2,
  storage_bytes: 1048576, storage_limit_mb: null, job_minutes_per_day: null };
const response = value => ({ ok: true, status: 200, json: async () => value });
let wire;
beforeEach(() => {
  forgetDeviceCredential();
  saveDeviceCredential({ key: 'u'.repeat(43), device: { device_id: 'device-admin', user_id: 'local-user' } });
  wire = vi.fn().mockImplementation(async url => response(String(url).endsWith('/audit') ? { items: [] }
    : { caller: admin, target: admin, items: [other] }));
  vi.stubGlobal('fetch', wire);
});
afterEach(() => { cleanup(); vi.unstubAllGlobals(); vi.restoreAllMocks(); forgetDeviceCredential(); });

it('shows an admin user row and selects another space without replacing the device identity', async () => {
  render(<SettingsUsers projectId="default"/>);
  await screen.findByText('乙');
  fireEvent.click(screen.getByRole('button', { name: '乙' }));
  fireEvent.click(screen.getByRole('button', { name: '进入乙的空间' }));
  expect(readUserSpace().user_id).toBe('user-b');
});

it('sends quota and disabled changes with the displayed user revision', async () => {
  wire.mockImplementation(async (url, options) => {
    if (options?.method === 'PATCH') return response({ ...other, revision: 3, disabled_at: 'now' });
    return response(String(url).endsWith('/audit') ? { items: [] } : { caller: admin, target: admin, items: [other] });
  });
  render(<SettingsUsers projectId="default"/>);
  await screen.findByText('乙');
  fireEvent.click(screen.getByRole('button', { name: '乙' }));
  fireEvent.click(screen.getByRole('switch', { name: '停用乙' }));
  await waitFor(() => expect(wire.mock.calls.some(([, options]) => options?.method === 'PATCH')).toBe(true));
  const [, options] = wire.mock.calls.find(([, value]) => value?.method === 'PATCH');
  expect(JSON.parse(options.body)).toEqual({ expected_revision: 2, disabled: true });
  await screen.findByText('已停用');
});

it('ordinary users see their administrator access audit without management actions', async () => {
  wire.mockImplementation(async url => response(String(url).endsWith('/audit') ? { items: [{ audit_id: 'audit-1',
    phase: 'intent', at: '2026-10-05T00:00:00Z', method: 'POST', path: '/api/v2/workbench/files' }] }
    : { caller: { ...other, role: 'user' }, target: other, items: [] }));
  render(<SettingsUsers projectId="default"/>);
  await screen.findByRole('button', { name: '访问记录' });
  fireEvent.click(screen.getByRole('button', { name: '访问记录' }));
  await screen.findByText('POST /api/v2/workbench/files');
  expect(screen.queryByRole('button', { name: '新建用户' })).toBeNull();
});

it('embeds users only in the server device settings group', async () => {
  wire.mockImplementation(async url => response(String(url).endsWith('/audit') ? { items: [] }
    : String(url).endsWith('/devices') ? { mode: 'server', device_id: 'device-admin', items: [] }
      : { caller: admin, target: admin, items: [other] }));
  render(<SettingsDevices projectId="default"/>);
  await screen.findByText('乙');
  expect(screen.getByRole('button', { name: '新建用户' })).toBeDisabled();
  expect(screen.getByRole('button', { name: '添加设备' })).toBeEnabled();
});
