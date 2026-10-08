import { act, cleanup, fireEvent, render, screen, within } from '@testing-library/react';
import { afterEach, beforeEach, expect, it, vi } from 'vitest';
import SettingsPage from '@src/features/settings/SettingsPage';

const defaults = () => ({ revision: 7, allow_remote: false, include_profile: true, daily_limit: 200, clients: { claude: true, codex: true } });
const copy = value => JSON.parse(JSON.stringify(value));
const response = (value, status = 200) => ({ ok: status < 400, status, json: async () => copy(value) });
const settle = async () => act(async () => {});
let value, writes, onRead, onWrite, proxyWrites, onProxyWrite;
beforeEach(() => {
  vi.stubEnv('DEV', false);
  value = { model: {
    generation: { revision: 3, model: 'writer', configured: true, enabled: true, allow_remote: false },
    generation_mode: { mode: 'api', revision: 2, local_enabled: false, local_model_installed: false },
    embedding: {}, rerank: {}, asr: {},
  }, privacy: { revision: 6, private_projects: [] }, external_agent: defaults(),
    external_proxy: { revision: 4, record_conversations: { claude: false, codex: false } } };
  writes = []; onRead = null; onWrite = null;
  proxyWrites = []; onProxyWrite = null;
  vi.stubGlobal('fetch', vi.fn(async (url, init = {}) => {
    const path = new URL(url, 'http://localhost').pathname;
    if (path === '/api/v2/settings' && (!init.method || init.method === 'GET')) return onRead ? onRead() : response(value);
    if (path === '/api/v2/projects') return response({ items: [] });
    if (path === '/api/v2/settings/external-proxy' && init.method === 'PATCH') {
      const body = JSON.parse(init.body); proxyWrites.push(body);
      if (onProxyWrite) return onProxyWrite(body);
      if (body.expected_revision !== value.external_proxy.revision) return response({ detail: 'external_proxy_revision_conflict' }, 409);
      value.external_proxy = { revision: body.expected_revision + 1, record_conversations: body.record_conversations };
      return response(value.external_proxy);
    }
    if (path === '/api/v2/settings/external-agent' && init.method === 'PATCH') {
      const body = JSON.parse(init.body); writes.push(body);
      if (onWrite) return onWrite(body);
      if (body.expected_revision !== value.external_agent.revision) return response({ detail: 'external_agent_revision_conflict' }, 409);
      const { expected_revision, ...fields } = body;
      value.external_agent = { ...fields, revision: expected_revision + 1 };
      return response(value.external_agent);
    }
    throw new Error('unexpected_synthetic_settings_request');
  }));
});
afterEach(() => { cleanup(); vi.unstubAllGlobals(); vi.unstubAllEnvs(); });
async function page(projectId = 'alpha') {
  const view = render(<SettingsPage key={projectId} projectId={projectId}/>);
  await settle(); return view;
}
function expand() { fireEvent.click(screen.getByRole('button', { name: '外部 agent', exact: true })); }

it('describes possible external retention without adding visible copy or changing the saved settings', async () => {
  await page();
  const explanation = '外部 agent 可能保留交出的内容';
  const section = screen.getByRole('button', { name: '外部 agent', exact: true }).closest('section');
  expect(section).toHaveAttribute('title', explanation);
  expect(screen.getByRole('region', { name: '外部 agent', exact: true })).toBe(section);
  expect(section).toHaveAccessibleDescription(explanation);
  const descriptionId = section.getAttribute('aria-describedby');
  const description = document.getElementById(descriptionId);
  expect(description).toHaveTextContent(explanation);
  expect(description).toHaveAttribute('hidden');
  expect(description).not.toBeVisible();
  expect(writes).toEqual([]);
  expand();
  fireEvent.click(screen.getByRole('switch', { name: '外部 agent外发' })); await settle();
  const { revision, ...fields } = defaults();
  expect(writes).toEqual([{ ...fields, allow_remote: true, expected_revision: revision }]);
  expect(screen.getByRole('switch', { name: '外部 agent外发' })).toHaveAttribute('aria-checked', 'true');
  expect(section).toHaveAccessibleDescription(explanation);
  expect(section).toHaveAttribute('aria-describedby', descriptionId);
  expect(description).not.toBeVisible();
});

it('renders independent external-agent preferences in the actual model group without installation or login claims', async () => {
  await page();
  const row = screen.getByRole('button', { name: '外部 agent', exact: true });
  expect(row).toHaveAttribute('aria-expanded', 'false');
  expect(screen.getByRole('switch', { name: '外部 agent外发' })).toHaveAttribute('aria-checked', 'false');
  expand();
  expect(row).toHaveAttribute('aria-expanded', 'true');
  expect(screen.getByRole('switch', { name: '带上画像' })).toHaveAttribute('aria-checked', 'true');
  expect(screen.getByRole('spinbutton', { name: '每日上限' })).toHaveValue(200);
  for (const client of ['Claude Code', 'Codex']) {
    expect(screen.getByRole('switch', { name: `${client}启用` })).toHaveAttribute('aria-checked', 'true');
    const clientRow = screen.getByText(client).closest('.ui-row');
    expect(within(clientRow).queryByText(/已安装|未安装|已登录/)).not.toBeInTheDocument();
  }
  expect(writes).toEqual([]);
});

it.each([
  ['外发', '外部 agent外发', { allow_remote: true }],
  ['画像', '带上画像', { include_profile: false }],
  ['Claude Code', 'Claude Code启用', { clients: { claude: false, codex: true } }],
  ['Codex', 'Codex启用', { clients: { claude: true, codex: false } }],
])('saves %s with the complete external settings and its own CAS revision', async (_, label, changes) => {
  await page(); expand();
  fireEvent.click(screen.getByRole('switch', { name: label })); await settle();
  const { revision, ...fields } = defaults();
  expect(writes).toEqual([{ ...fields, ...changes, expected_revision: revision }]);
  expect(value.model.generation.revision).toBe(3); expect(value.privacy.revision).toBe(6);
  expect(screen.getByRole('switch', { name: label })).toHaveAttribute('aria-checked', String(label === '外部 agent外发'));
});

it('saves a numeric daily limit while preserving both clients and authorization', async () => {
  await page(); expand();
  fireEvent.change(screen.getByRole('spinbutton', { name: '每日上限' }), { target: { value: '321' } });
  fireEvent.click(screen.getByRole('button', { name: '保存每日上限' })); await settle();
  const { revision, ...fields } = defaults();
  expect(writes).toEqual([{ ...fields, daily_limit: 321, expected_revision: revision }]);
  expect(screen.getByRole('spinbutton', { name: '每日上限' })).toHaveValue(321);
});

it('disables invalid daily limits without sending a replacement', async () => {
  await page(); expand();
  for (const invalid of ['', '0', '-1', '1.5']) {
    fireEvent.change(screen.getByRole('spinbutton', { name: '每日上限' }), { target: { value: invalid } });
    expect(screen.getByRole('button', { name: '保存每日上限' })).toBeDisabled();
    fireEvent.click(screen.getByRole('button', { name: '保存每日上限' }));
  }
  expect(writes).toEqual([]);
});

it('shares the existing busy lock across settings controls until the real request resolves', async () => {
  let finish;
  onWrite = body => new Promise(resolve => { finish = () => {
    const { expected_revision, ...fields } = body; value.external_agent = { ...fields, revision: expected_revision + 1 };
    resolve(response(value.external_agent));
  }; });
  await page(); expand(); fireEvent.click(screen.getByRole('switch', { name: '带上画像' })); await settle();
  for (const label of ['外部 agent外发', '带上画像', 'Claude Code启用', 'Codex启用', '生成外发']) expect(screen.getByRole('switch', { name: label })).toBeDisabled();
  expect(screen.getByRole('spinbutton', { name: '每日上限' })).toBeDisabled();
  fireEvent.click(screen.getByRole('switch', { name: 'Codex启用' })); expect(writes).toHaveLength(1);
  await act(async () => finish());
  expect(screen.getByRole('switch', { name: '带上画像' })).not.toBeDisabled();
  expect(screen.getByRole('switch', { name: '带上画像' })).toHaveAttribute('aria-checked', 'false');
});

it('shows the original 409 feedback and reloads actual preferences before the next write', async () => {
  await page(); expand();
  value.external_agent = { ...defaults(), revision: 8, daily_limit: 432, clients: { claude: false, codex: true } };
  fireEvent.click(screen.getByRole('switch', { name: '带上画像' })); await settle();
  expect(screen.getByRole('alert')).toHaveTextContent('设置已变化 · 刷新');
  expect(screen.getByRole('spinbutton', { name: '每日上限' })).toHaveValue(432);
  expect(screen.getByRole('switch', { name: 'Claude Code启用' })).toHaveAttribute('aria-checked', 'false');
  fireEvent.click(screen.getByRole('switch', { name: 'Codex启用' })); await settle();
  expect(writes[1]).toMatchObject({ expected_revision: 8, daily_limit: 432, clients: { claude: false, codex: false } });
});

it('ignores a late settings read after the routed project instance is replaced', async () => {
  let finish;
  const old = copy(value);
  onRead = () => new Promise(resolve => { finish = () => resolve(response(old)); });
  const view = await page();
  value.external_agent = { ...defaults(), revision: 12, daily_limit: 987 };
  onRead = null;
  view.rerender(<SettingsPage key="beta" projectId="beta"/>); await settle(); expand();
  expect(screen.getByRole('spinbutton', { name: '每日上限' })).toHaveValue(987);
  await act(async () => finish());
  expect(screen.getByRole('spinbutton', { name: '每日上限' })).toHaveValue(987);
  expect(writes).toEqual([]);
});

it('does not refresh or report a late failed write into the next routed project instance', async () => {
  let finish;
  onWrite = () => new Promise(resolve => { finish = () => resolve(response({ detail: 'revision_conflict' }, 409)); });
  const view = await page(); expand(); fireEvent.click(screen.getByRole('switch', { name: '带上画像' })); await settle();
  value.external_agent = { ...defaults(), revision: 12, daily_limit: 987 };
  view.rerender(<SettingsPage key="beta" projectId="beta"/>); await settle(); expand();
  const reads = fetch.mock.calls.filter(([url]) => String(url).endsWith('/api/v2/settings')).length;
  await act(async () => finish());
  expect(screen.queryByRole('alert')).not.toBeInTheDocument();
  expect(screen.getByRole('spinbutton', { name: '每日上限' })).toHaveValue(987);
  expect(screen.getByRole('switch', { name: '带上画像' })).not.toBeDisabled();
  expect(fetch.mock.calls.filter(([url]) => String(url).endsWith('/api/v2/settings'))).toHaveLength(reads);
});

it('expands the mono proxy root with two default-off recording switches', async () => {
  await page(); expand();
  expect(screen.queryByRole('switch', { name: 'Claude Code记录对话' })).not.toBeInTheDocument();
  fireEvent.click(screen.getByRole('button', { name: '代理接入', exact: true }));
  const address = screen.getByText(`${globalThis.location.origin}/api/v2/external-agent/proxy`);
  expect(address).toHaveClass('ui-row-meta');
  expect(address).toHaveAccessibleDescription(/claude\/v1\/messages/);
  for (const client of ['Claude Code', 'Codex']) expect(screen.getByRole('switch', { name: `${client}记录对话` })).toHaveAttribute('aria-checked', 'false');
  expect(proxyWrites).toEqual([]);
});

it.each([
  { mode: 'production', dev: false, origin: 'https://memory.example.test:8443', expected: 'https://memory.example.test:8443' },
  { mode: 'development', dev: true, origin: 'http://localhost:4173', backend: 'http://127.0.0.1:9107', expected: 'http://127.0.0.1:9107' },
  { mode: 'electron', dev: true, origin: 'null', backend: 'http://127.0.0.1:9107', electron: 'http://127.0.0.1:9312', expected: 'http://127.0.0.1:9312' },
])('shows the actual absolute proxy address for $mode', async ({ dev, origin, backend, electron, expected }) => {
  vi.stubEnv('DEV', dev);
  vi.stubGlobal('location', { origin, href: origin === 'null' ? 'file:///app/index.html' : `${origin}/` });
  const configuredBackend = backend && (typeof __BACKEND_PROXY_ORIGIN__ === 'undefined' ? backend : __BACKEND_PROXY_ORIGIN__);
  if (backend) vi.stubGlobal('__BACKEND_PROXY_ORIGIN__', configuredBackend);
  if (electron) vi.stubGlobal('electronAPI', { backendBaseUrl: electron });
  await page(); expand();
  const expectedOrigin = dev && !electron ? configuredBackend : expected;
  const address = screen.getByText(`${expectedOrigin}/api/v2/external-agent/proxy`);
  expect(address).toHaveClass('ui-row-meta');
  expect(address).toHaveAttribute('title', `${expectedOrigin}/api/v2/external-agent/proxy/claude/v1/messages · ${expectedOrigin}/api/v2/external-agent/proxy/codex/v1/responses`);
  expect(address).toHaveAccessibleDescription(address.getAttribute('title'));
  expect(proxyWrites).toEqual([]);
  expect(writes).toEqual([]);
});

it.each(['Claude Code', 'Codex'])('saves %s recording using separate revision without changing external authorization', async client => {
  await page(); expand(); fireEvent.click(screen.getByRole('button', { name: '代理接入', exact: true }));
  fireEvent.click(screen.getByRole('switch', { name: `${client}记录对话` })); await settle();
  expect(proxyWrites).toEqual([{ expected_revision: 4, record_conversations: { claude: client === 'Claude Code', codex: client === 'Codex' } }]);
  expect(writes).toEqual([]); expect(value.external_agent).toEqual(defaults());
  expect(screen.getByRole('switch', { name: `${client}记录对话` })).toHaveAttribute('aria-checked', 'true');
});

it('reloads recording CAS conflicts and uses the current proxy revision', async () => {
  await page(); expand(); fireEvent.click(screen.getByRole('button', { name: '代理接入', exact: true }));
  value.external_proxy = { revision: 5, record_conversations: { claude: true, codex: false } };
  fireEvent.click(screen.getByRole('switch', { name: 'Codex记录对话' })); await settle();
  expect(screen.getByRole('alert')).toHaveTextContent('设置已变化 · 刷新');
  expect(screen.getByRole('switch', { name: 'Claude Code记录对话' })).toHaveAttribute('aria-checked', 'true');
  fireEvent.click(screen.getByRole('switch', { name: 'Codex记录对话' })); await settle();
  expect(proxyWrites[1]).toEqual({ expected_revision: 5, record_conversations: { claude: true, codex: true } });
});

it('shares the settings busy lock and rejects a late proxy error after project replacement', async () => {
  let finish;
  onProxyWrite = () => new Promise(resolve => { finish = () => resolve(response({ detail: 'revision_conflict' }, 409)); });
  const view = await page(); expand(); fireEvent.click(screen.getByRole('button', { name: '代理接入', exact: true }));
  fireEvent.click(screen.getByRole('switch', { name: 'Codex记录对话' })); await settle();
  expect(screen.getByRole('switch', { name: 'Claude Code记录对话' })).toBeDisabled();
  expect(screen.getByRole('switch', { name: '外部 agent外发' })).toBeDisabled();
  value.external_proxy = { revision: 9, record_conversations: { claude: true, codex: false } };
  view.rerender(<SettingsPage key="beta" projectId="beta"/>); await settle(); expand();
  fireEvent.click(screen.getByRole('button', { name: '代理接入', exact: true }));
  await act(async () => finish());
  expect(screen.queryByRole('alert')).not.toBeInTheDocument();
  expect(screen.getByRole('switch', { name: 'Claude Code记录对话' })).toHaveAttribute('aria-checked', 'true');
  expect(screen.getByRole('switch', { name: 'Codex记录对话' })).not.toBeDisabled();
});
