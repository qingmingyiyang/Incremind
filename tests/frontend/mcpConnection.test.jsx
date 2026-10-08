import { act, cleanup, fireEvent, render, screen } from '@testing-library/react';
import { afterEach, beforeEach, expect, it, vi } from 'vitest';
import SettingsPage from '@src/features/settings/SettingsPage';
import { settingsApi } from '@src/features/settings/settingsApi';

const pending = () => { let resolve, reject; const promise = new Promise((yes, no) => { resolve = yes; reject = no; }); return { promise, resolve, reject }; };
const settle = async () => act(async () => {});
const defaults = () => ({ model: {
  generation: { revision: 3, model: 'writer', configured: true, enabled: true, allow_remote: false },
  generation_mode: { mode: 'api', revision: 2, local_enabled: false, local_model_installed: false },
  embedding: {}, rerank: {}, asr: {},
}, privacy: { revision: 6, private_projects: [] }, external_agent: {
  revision: 7, allow_remote: false, include_profile: true, daily_limit: 200, clients: { claude: true, codex: true },
} });
const metadata = project => ({ available: true, project_id: project, shell: 'powershell', version: 'mcp-connection@1',
  commands: { claude: `claude safe ${project}`, codex: `codex safe ${project}` },
  instructions: `mcp-connection@1 methods recall project="${project}" report_use(turn_id, ids)` });
let api, copy;
beforeEach(() => {
  api = { load: vi.fn(async () => defaults()), projects: vi.fn(async () => ({ items: [] })),
    connection: vi.fn(async project => metadata(project)), saveExternalAgent: vi.fn(async () => ({})) };
  copy = vi.fn(async () => {});
  Object.defineProperty(navigator, 'clipboard', { configurable: true, value: { writeText: copy } });
});
afterEach(() => { cleanup(); vi.restoreAllMocks(); vi.unstubAllGlobals(); });
async function page(projectId = 'alpha') {
  const view = render(<SettingsPage projectId={projectId} api={api}/>); await settle();
  fireEvent.click(screen.getByRole('button', { name: '外部 agent', exact: true }));
  return view;
}
function open(client = 'Claude Code') { fireEvent.click(screen.getByRole('button', { name: client, exact: true })); }

it('loads only on client expansion and copies commands and the versioned template without settings writes', async () => {
  await page(); expect(api.connection).not.toHaveBeenCalled(); open(); await settle();
  expect(api.connection).toHaveBeenCalledWith('alpha', expect.any(AbortSignal));
  expect(screen.getByText('claude safe alpha')).toBeVisible();
  fireEvent.click(screen.getByRole('button', { name: '复制Claude Code命令' })); await settle();
  expect(copy).toHaveBeenLastCalledWith('claude safe alpha');
  fireEvent.click(screen.getByRole('button', { name: '复制说明' })); await settle();
  expect(copy).toHaveBeenLastCalledWith(metadata('alpha').instructions);
  open('Codex'); await settle();
  fireEvent.click(screen.getByRole('button', { name: '复制Codex命令' })); await settle();
  expect(copy).toHaveBeenLastCalledWith('codex safe alpha');
  expect(api.saveExternalAgent).not.toHaveBeenCalled();
  expect(screen.queryByText(/已安装|未安装|已登录/)).not.toBeInTheDocument();
  expect(screen.getByRole('button', { name: '复制快照' })).toBeDisabled();
});

it('preserves client switches and the complete independent CAS payload', async () => {
  await page(); open(); await settle();
  fireEvent.click(screen.getByRole('switch', { name: 'Claude Code启用' })); await settle();
  const { revision, ...fields } = defaults().external_agent;
  expect(api.saveExternalAgent).toHaveBeenCalledWith({ ...fields, clients: { claude: false, codex: true }, expected_revision: revision });
});

it('shows copy failure in Chinese and allows a retry', async () => {
  copy.mockRejectedValueOnce(new Error('private_clipboard_details'));
  await page(); open(); await settle();
  fireEvent.click(screen.getByRole('button', { name: '复制Claude Code命令' })); await settle();
  expect(screen.getByRole('alert')).toHaveTextContent('复制未完成 · 重试');
  expect(screen.queryByText('private_clipboard_details')).not.toBeInTheDocument();
  fireEvent.click(screen.getByRole('button', { name: '复制Claude Code命令' })); await settle();
  expect(copy).toHaveBeenCalledTimes(2); expect(screen.queryByRole('alert')).not.toBeInTheDocument();
});

it('cannot copy before metadata arrives and exposes a retry after read failure', async () => {
  const read = pending(); api.connection.mockReturnValueOnce(read.promise);
  await page(); open(); await settle();
  expect(screen.queryByRole('button', { name: /复制/ })).not.toBeInTheDocument();
  await act(async () => read.reject(new Error('private_read_details')));
  expect(screen.getByRole('alert')).toHaveTextContent('读取未完成 · 重试');
  fireEvent.click(screen.getByRole('button', { name: '重试连接说明' })); await settle();
  expect(screen.getByText('claude safe alpha')).toBeVisible(); expect(copy).not.toHaveBeenCalled();
});

it('hides desktop metadata and copy controls in server mode', async () => {
  api.connection.mockResolvedValue({ available: false });
  await page(); open(); await settle();
  expect(screen.queryByRole('button', { name: /复制/ })).not.toBeInTheDocument();
  expect(screen.queryByText('连接说明')).not.toBeInTheDocument();
  expect(screen.getByRole('switch', { name: 'Claude Code启用' })).toBeVisible();
});

it('aborts and rejects old metadata on project change without a keyed parent remount', async () => {
  const old = pending(); api.connection.mockReturnValueOnce(old.promise);
  const view = await page(); open(); await settle();
  const signal = api.connection.mock.calls[0][1];
  view.rerender(<SettingsPage projectId="beta" api={api}/>); await settle();
  expect(signal.aborted).toBe(true);
  fireEvent.click(screen.getByRole('button', { name: '外部 agent', exact: true })); open(); await settle();
  await act(async () => old.resolve(metadata('alpha')));
  expect(screen.queryByText('claude safe alpha')).not.toBeInTheDocument();
  expect(screen.getByText('claude safe beta')).toBeVisible();
});

it.each(['resolve', 'reject'])('ignores late clipboard %s after project change', async outcome => {
  const old = pending(); copy.mockReturnValueOnce(old.promise);
  const view = await page(); open(); await settle();
  fireEvent.click(screen.getByRole('button', { name: '复制Claude Code命令' }));
  view.rerender(<SettingsPage projectId="beta" api={api}/>); await settle();
  await act(async () => outcome === 'resolve' ? old.resolve() : old.reject(new Error('late')));
  expect(screen.queryByText('已复制')).not.toBeInTheDocument();
  expect(screen.queryByRole('alert')).not.toBeInTheDocument();
  expect(screen.queryByText('claude safe alpha')).not.toBeInTheDocument();
});

it('aborts pending metadata on unmount and ignores its result', async () => {
  const read = pending(); api.connection.mockReturnValueOnce(read.promise);
  const view = await page(); open(); await settle();
  const signal = api.connection.mock.calls[0][1]; view.unmount();
  expect(signal.aborted).toBe(true);
  await act(async () => read.resolve(metadata('alpha')));
  expect(copy).not.toHaveBeenCalled();
});

it('reads through the actual desktop settings transport with the current project and abort signal', async () => {
  const fetch = vi.fn(async () => ({ ok: true, status: 200, json: async () => metadata('alpha~1') }));
  vi.stubGlobal('fetch', fetch);
  vi.stubGlobal('electronAPI', { backendBaseUrl: 'http://127.0.0.1:8765' });
  const controller = new AbortController();
  expect(await settingsApi.connection('alpha~1', controller.signal)).toEqual(metadata('alpha~1'));
  expect(fetch).toHaveBeenCalledExactlyOnceWith(
    'http://127.0.0.1:8765/api/v2/settings/external-agent/connection?project_id=alpha~1',
    { method: 'GET', cache: 'no-store', headers: { 'Content-Type': 'application/json' }, signal: controller.signal });
});

it('refuses foreign-project metadata without displaying or copying its command', async () => {
  api.connection.mockResolvedValue(metadata('beta'));
  await page(); open(); await settle();
  expect(screen.getByRole('alert')).toHaveTextContent('读取未完成 · 重试');
  expect(screen.queryByText('claude safe beta')).not.toBeInTheDocument();
  expect(screen.queryByRole('button', { name: /复制/ })).not.toBeInTheDocument();
  expect(copy).not.toHaveBeenCalled(); expect(api.saveExternalAgent).not.toHaveBeenCalled();
});
