import { render, screen, fireEvent, waitFor } from '@testing-library/react';
import { vi, afterEach, beforeEach, test, expect } from 'vitest';
import { ChatGPTSubscriptionSettings } from '../../src/frontend/src/shared/ui/ChatGPTSubscriptionSettings';

let status, progress, requests, popup;
beforeEach(() => {
  status = { connected: false, sharing: false, revision: 0, allow_remote: false, generation_revision: 2,
    selection: { model: null, revision: 0 } };
  progress = { state: 'pending' }; requests = [];
  popup = { location: { href: '' }, close: vi.fn(), opener: null };
  vi.spyOn(window, 'open').mockReturnValue(popup);
  vi.stubGlobal('fetch', vi.fn(async (url, options = {}) => {
    requests.push([url, options.body ? JSON.parse(options.body) : null, options.method]);
    let value = status;
    if (url.endsWith('/login')) value = { attempt_id: 'attempt', authorization_url: 'https://auth.openai.com/authorize' };
    if (url.endsWith('/login/attempt')) value = progress;
    if (url.endsWith('/models')) value = { items: [{ id: 'gpt-fixture' }] };
    if (url.endsWith('/logout')) { status = { ...status, connected: false, sharing: false, revision: 2 }; value = { remote_revoked: false }; }
    if (url.endsWith('/selection')) { status.selection = { model: JSON.parse(options.body).model, revision: 1 }; value = status.selection; }
    return { ok: true, status: 200, json: async () => value };
  }));
});
afterEach(() => { vi.restoreAllMocks(); vi.unstubAllGlobals(); });

test('starts login from a user gesture and polls completion without enabling egress', async () => {
  render(<ChatGPTSubscriptionSettings />);
  fireEvent.click(await screen.findByRole('button', { name: '登录' }));
  await waitFor(() => expect(popup.location.href).toBe('https://auth.openai.com/authorize'));
  expect(window.open).toHaveBeenCalledWith('about:blank', '_blank');
  status = { ...status, connected: true, sharing: true, revision: 1, identity: { email: 'qa@example.invalid' } };
  progress = { state: 'completed' };
  await screen.findByText('qa@example.invalid');
  expect(screen.getByRole('switch')).toHaveAttribute('aria-checked', 'false');
  expect(requests.filter(([url]) => url.endsWith('/selection'))).toHaveLength(0);
});

test('selects a catalog model with CAS and shows the global switch', async () => {
  status = { ...status, connected: true, sharing: true, revision: 1 };
  render(<ChatGPTSubscriptionSettings />);
  await screen.findByRole('option', { name: 'gpt-fixture' });
  fireEvent.change(screen.getByRole('combobox'), { target: { value: 'gpt-fixture' } });
  await waitFor(() => expect(requests).toContainEqual(['/api/v2/settings/subscriptions/selection', { model: 'gpt-fixture', expected_revision: 0 }, 'PATCH']));
  fireEvent.click(screen.getByRole('switch'));
  await waitFor(() => expect(requests).toContainEqual(['/api/v2/settings/subscriptions/selection',
    { model: 'gpt-fixture', expected_revision: 1, allow_remote: true, expected_generation_revision: 2 }, 'PATCH']));
});

test('clears local login and shows an unconfirmed remote logout', async () => {
  status = { ...status, connected: true, sharing: true, revision: 1 };
  render(<ChatGPTSubscriptionSettings />);
  fireEvent.click(await screen.findByRole('button', { name: '退出' }));
  await screen.findByRole('link', { name: '查看账号' });
  expect(await screen.findByRole('button', { name: '登录' })).toBeVisible();
  expect(requests).toContainEqual(['/api/v2/settings/subscriptions/logout', { expected_revision: 1 }, 'POST']);
});

test('cancels the active attempt', async () => {
  render(<ChatGPTSubscriptionSettings />);
  fireEvent.click(await screen.findByRole('button', { name: '登录' }));
  fireEvent.click(await screen.findByRole('button', { name: '取消' }));
  await waitFor(() => expect(requests).toContainEqual(['/api/v2/settings/subscriptions/login/attempt', null, 'DELETE']));
  expect(popup.close).toHaveBeenCalled();
});

test('safe one-line errors never display provider payloads or credentials', async () => {
  fetch.mockResolvedValue({ ok: false, status: 502, json: async () => ({ detail: 'synthetic-private-token' }) });
  render(<ChatGPTSubscriptionSettings />);
  expect(await screen.findByRole('alert')).toHaveTextContent('连接未完成 · 重试');
  expect(screen.queryByText('synthetic-private-token')).toBeNull();
});
