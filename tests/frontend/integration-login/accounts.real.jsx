import { afterAll, expect, it } from 'vitest';
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react';
import { readFile } from 'node:fs/promises';
import { SettingsUsers } from '@src/features/settings/SettingsUsers';
import { devicesApi } from '@src/features/settings/devicesApi';
import { forgetDeviceCredential, saveDeviceCredential } from '@src/shared/api/deviceTransport';

const nativeFetch = globalThis.fetch;
afterAll(() => { cleanup(); forgetDeviceCredential(); globalThis.fetch = nativeFetch; });

it('creates a real password account and resets it through the admin form with actual server revocation', async () => {
  const path = process.env.CHRIPTMAS_LOGIN_QA_CREDENTIALS;
  if (!path) throw new Error('CHRIPTMAS_LOGIN_QA_CREDENTIALS_required');
  const credentials = JSON.parse(await readFile(path, 'utf8'));
  const origin = 'http://127.0.0.1:8001';
  // 仅将同源相对请求发送到已启动的真实隔离服务。
  globalThis.fetch = (input, options) => nativeFetch(new URL(input, origin), options);
  saveDeviceCredential({ key: credentials.device_key, device: { device_id: credentials.device_id, user_id: 'local-user' } });
  const name = '登录界面检查' + Date.now(), account = 'ui.' + Date.now();
  const password = 'QA-initial-password-20261024', replacement = 'QA-replacement-password-20261024';
  render(<SettingsUsers projectId="default"/>);
  fireEvent.change(await screen.findByLabelText('新用户名称'), { target: { value: name } });
  fireEvent.change(screen.getByLabelText('新用户账号'), { target: { value: account } });
  fireEvent.change(screen.getByLabelText('新用户初始密码'), { target: { value: password } });
  expect(screen.getByLabelText('新用户账号').validity.patternMismatch).toBe(false);
  fireEvent.click(screen.getByRole('button', { name: '新建用户', exact: true }));
  await screen.findByRole('button', { name, exact: true });
  await waitFor(() => expect(screen.getByLabelText('新用户初始密码')).toHaveValue(''));
  const priorSession = await devicesApi.login(account, password);
  const denied = await nativeFetch(origin + '/api/v2/server/users', { method: 'POST',
    headers: { Authorization: 'Bearer ' + priorSession.key, 'Content-Type': 'application/json' }, body: JSON.stringify({ name: '禁止新增' }) });
  expect(denied.status).toBe(403);
  fireEvent.click(screen.getByRole('button', { name: `管理${name}`, exact: true }));
  expect(screen.getByLabelText(`${name}登录账号`)).toHaveValue(account);
  fireEvent.change(screen.getByLabelText(`${name}新密码`), { target: { value: replacement } });
  fireEvent.click(screen.getByRole('button', { name: '保存登录账号', exact: true }));
  await screen.findByRole('status');
  await waitFor(() => expect(screen.getByLabelText(`${name}新密码`)).toHaveValue(''));
  const revoked = await nativeFetch(origin + '/api/v2/devices', { headers: { Authorization: 'Bearer ' + priorSession.key } });
  expect(revoked.status).toBe(401);
  const next = await devicesApi.login(account, replacement);
  const logout = await nativeFetch(origin + '/api/v2/devices/logout', { method: 'POST',
    headers: { Authorization: 'Bearer ' + next.key, 'Content-Type': 'application/json' }, body: '{}' });
  expect(logout.status).toBe(200);
  const afterLogout = await nativeFetch(origin + '/api/v2/devices', { headers: { Authorization: 'Bearer ' + next.key } });
  expect(afterLogout.status).toBe(401);
});
