import { act, cleanup, fireEvent, render, screen } from '@testing-library/react';
import { afterEach, beforeEach, expect, it, vi } from 'vitest';
import { ExternalAgentSettings } from '@src/features/settings/ExternalAgentSettings';
import { settingsApi } from '@src/features/settings/settingsApi';

const deferred = () => { let resolve, reject; const promise = new Promise((yes, no) => { resolve = yes; reject = no; }); return { promise, resolve, reject }; };
const settle = async () => act(async () => {});
const preferences = { revision: 7, allow_remote: true, include_profile: true, daily_limit: 200, clients: { claude: true, codex: true } };
const begin = '<!-- chriptmas-memory:external-snapshot@1:begin -->';
const end = '<!-- chriptmas-memory:external-snapshot@1:end -->';
const snapshot = (project = 'alpha', client = 'claude') => ({ version: 'external-snapshot@1', project_id: project,
  client, generated_at: '2026-10-06T01:00:00Z', turn_id: `turn-${'a'.repeat(32)}`, text: `${begin}\n已确认的合成画像与本项目方法\n${end}` });
let api, copy;
beforeEach(() => {
  api = { connection: vi.fn(async project => ({ available: true, project_id: project, version: 'mcp-connection@1', shell: 'powershell',
    commands: { claude: 'claude safe command', codex: 'codex safe command' }, instructions: 'mcp-connection@1' })),
    snapshot: vi.fn(async (body) => snapshot(body.project_id, body.client)), saveExternalAgent: vi.fn() };
  copy = vi.fn(async () => {});
  Object.defineProperty(navigator, 'clipboard', { configurable: true, value: { writeText: copy } });
});
afterEach(() => { cleanup(); vi.restoreAllMocks(); vi.unstubAllGlobals(); });
const props = changes => ({ projectId: 'alpha', value: preferences, expanded: true, onOpen: vi.fn(), api, run: vi.fn(), busy: false, ...changes });
async function page() {
  const view = render(<ExternalAgentSettings {...props()}/>);
  fireEvent.click(screen.getByRole('button', { name: 'Claude Code', exact: true })); await settle();
  return view;
}

it('generates only on explicit copy and copies the marked timestamped snapshot for this client', async () => {
  await page(); expect(api.snapshot).not.toHaveBeenCalled();
  fireEvent.click(screen.getByRole('button', { name: '复制快照' })); await settle();
  expect(api.snapshot).toHaveBeenCalledWith({ client: 'claude', project_id: 'alpha', budget: 3000 }, expect.any(AbortSignal));
  expect(copy).toHaveBeenCalledWith(snapshot().text);
  expect(screen.getByRole('button', { name: '复制快照' })).toHaveTextContent('已复制');
  expect(api.saveExternalAgent).not.toHaveBeenCalled();
});

it('shows the qualified generation time beside the snapshot copy button only after copying', async () => {
  await page();
  expect(screen.queryByLabelText('快照生成时间')).not.toBeInTheDocument();
  fireEvent.click(screen.getByRole('button', { name: '复制快照' })); await settle();
  const time = screen.getByLabelText('快照生成时间');
  expect(time.tagName).toBe('TIME');
  expect(time).toHaveAttribute('datetime', snapshot().generated_at);
  expect(time).toHaveTextContent(/^\d{2}-\d{2} \d{2}:\d{2}$/);
  expect(screen.getByRole('button', { name: '复制快照' }).nextElementSibling).toBe(time);
  expect(time.parentElement).toHaveClass('settings-test');
  expect(copy).toHaveBeenCalledExactlyOnceWith(snapshot().text);
});

it.each(['project', 'client', 'revision'])('clears the old time and rejects a late snapshot after changing %s', async change => {
  const view = await page();
  fireEvent.click(screen.getByRole('button', { name: '复制快照' })); await settle();
  expect(screen.getByLabelText('快照生成时间')).toHaveAttribute('datetime', snapshot().generated_at);
  const old = deferred(); api.snapshot.mockReturnValueOnce(old.promise);
  fireEvent.click(screen.getByRole('button', { name: '复制快照' })); await settle();
  expect(screen.queryByLabelText('快照生成时间')).not.toBeInTheDocument();
  const signal = api.snapshot.mock.calls[1][1];
  if (change === 'client') fireEvent.click(screen.getByRole('button', { name: 'Codex', exact: true }));
  else view.rerender(<ExternalAgentSettings {...props(change === 'project' ? { projectId: 'beta' }
    : { value: { ...preferences, revision: 8 } })}/>);
  await settle();
  expect(signal.aborted).toBe(true);
  await act(async () => old.resolve({ ...snapshot(), generated_at: '2026-10-06T02:00:00Z' }));
  expect(screen.queryByLabelText('快照生成时间')).not.toBeInTheDocument();
  expect(copy).toHaveBeenCalledTimes(1);
  const project = change === 'project' ? 'beta' : 'alpha', client = change === 'client' ? 'codex' : 'claude';
  const current = { ...snapshot(project, client), generated_at: '2026-10-06T03:00:00Z' };
  api.snapshot.mockResolvedValueOnce(current);
  fireEvent.click(screen.getByRole('button', { name: '复制快照' })); await settle();
  expect(screen.getByLabelText('快照生成时间')).toHaveAttribute('datetime', current.generated_at);
  expect(api.snapshot).toHaveBeenLastCalledWith({ client, project_id: project, budget: 3000 }, expect.any(AbortSignal));
});

it.each(['generation', 'timestamp', 'clipboard'])('keeps the old time cleared when a fresh %s fails', async failure => {
  await page();
  fireEvent.click(screen.getByRole('button', { name: '复制快照' })); await settle();
  expect(screen.getByLabelText('快照生成时间')).toHaveAttribute('datetime', snapshot().generated_at);
  const next = deferred(); api.snapshot.mockReturnValueOnce(next.promise);
  if (failure === 'clipboard') copy.mockRejectedValueOnce(new Error('synthetic_private_copy_failure'));
  fireEvent.click(screen.getByRole('button', { name: '复制快照' })); await settle();
  expect(screen.queryByLabelText('快照生成时间')).not.toBeInTheDocument();
  await act(async () => failure === 'generation' ? next.reject(new Error('synthetic_private_generation_failure'))
    : next.resolve({ ...snapshot(), generated_at: failure === 'timestamp' ? 'invalid' : '2026-10-06T02:00:00Z' }));
  expect(screen.getByRole('alert')).toHaveTextContent(failure === 'clipboard' ? '复制未完成 · 重试' : '生成未完成 · 重试');
  expect(screen.queryByLabelText('快照生成时间')).not.toBeInTheDocument();
  expect(copy).toHaveBeenCalledTimes(failure === 'clipboard' ? 2 : 1);
  expect(screen.queryByText(/synthetic_private/)).not.toBeInTheDocument();
});

it.each(['project', 'client', 'revision'])('does not publish a late clipboard success time into a changed %s', async change => {
  const view = await page();
  fireEvent.click(screen.getByRole('button', { name: '复制快照' })); await settle();
  expect(screen.getByLabelText('快照生成时间')).toBeVisible();
  const pendingCopy = deferred(); copy.mockReturnValueOnce(pendingCopy.promise);
  api.snapshot.mockResolvedValueOnce({ ...snapshot(), generated_at: '2026-10-06T02:00:00Z' });
  fireEvent.click(screen.getByRole('button', { name: '复制快照' })); await settle();
  expect(screen.queryByLabelText('快照生成时间')).not.toBeInTheDocument();
  const signal = api.snapshot.mock.calls[1][1];
  if (change === 'client') fireEvent.click(screen.getByRole('button', { name: 'Codex', exact: true }));
  else view.rerender(<ExternalAgentSettings {...props(change === 'project' ? { projectId: 'beta' }
    : { value: { ...preferences, revision: 8 } })}/>);
  await settle();
  expect(signal.aborted).toBe(true);
  await act(async () => pendingCopy.resolve());
  expect(screen.queryByLabelText('快照生成时间')).not.toBeInTheDocument();
  expect(screen.getByRole('button', { name: '复制快照' })).toHaveTextContent('复制快照');
  expect(copy).toHaveBeenCalledTimes(2);
});

it('rejects foreign or unmarked snapshot responses before clipboard writes', async () => {
  api.snapshot.mockResolvedValueOnce(snapshot('beta')).mockResolvedValueOnce({ ...snapshot(), text: 'unqualified' });
  await page();
  for (let index = 0; index < 2; index++) {
    fireEvent.click(screen.getByRole('button', { name: '复制快照' })); await settle();
    expect(screen.getByRole('alert')).toHaveTextContent('生成未完成 · 重试');
  }
  expect(copy).not.toHaveBeenCalled();
});

it('does not release a late snapshot after changing project or the external setting revision', async () => {
  const first = deferred(), second = deferred(); api.snapshot.mockReturnValueOnce(first.promise).mockReturnValueOnce(second.promise);
  const view = await page();
  fireEvent.click(screen.getByRole('button', { name: '复制快照' })); await settle();
  const firstSignal = api.snapshot.mock.calls[0][1];
  view.rerender(<ExternalAgentSettings {...props({ projectId: 'beta' })}/>); await settle();
  await act(async () => first.resolve(snapshot()));
  expect(firstSignal.aborted).toBe(true); expect(copy).not.toHaveBeenCalled();
  fireEvent.click(screen.getByRole('button', { name: '复制快照' })); await settle();
  view.rerender(<ExternalAgentSettings {...props({ projectId: 'beta', value: { ...preferences, revision: 8, include_profile: false } })}/>); await settle();
  await act(async () => second.resolve(snapshot('beta')));
  expect(copy).not.toHaveBeenCalled();
});

it('shows a clipboard error and retries through a fresh qualified generation', async () => {
  copy.mockRejectedValueOnce(new Error('synthetic_private_clipboard_error'));
  await page(); fireEvent.click(screen.getByRole('button', { name: '复制快照' })); await settle();
  expect(screen.getByRole('alert')).toHaveTextContent('复制未完成 · 重试');
  expect(screen.queryByText('synthetic_private_clipboard_error')).not.toBeInTheDocument();
  fireEvent.click(screen.getByRole('button', { name: '复制快照' })); await settle();
  expect(api.snapshot).toHaveBeenCalledTimes(2); expect(copy).toHaveBeenCalledTimes(2);
});

it('has no snapshot action when desktop connection is unavailable', async () => {
  api.connection.mockResolvedValue({ available: false });
  await page(); expect(screen.queryByRole('button', { name: '复制快照' })).not.toBeInTheDocument();
  expect(api.snapshot).not.toHaveBeenCalled();
});

it('uses the actual snapshot POST with the abort signal', async () => {
  const signal = new AbortController().signal;
  const fetcher = vi.fn(async () => ({ ok: true, json: async () => snapshot() })); vi.stubGlobal('fetch', fetcher);
  const body = { client: 'claude', project_id: 'alpha', budget: 3000 };
  expect(await settingsApi.snapshot(body, signal)).toEqual(snapshot());
  expect(fetcher).toHaveBeenCalledWith(expect.stringContaining('/api/v2/settings/external-agent/snapshot'),
    expect.objectContaining({ method: 'POST', body: JSON.stringify(body), signal }));
});
