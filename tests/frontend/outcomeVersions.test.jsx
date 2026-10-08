import { act, cleanup, fireEvent, render, screen } from '@testing-library/react';
import { afterEach, expect, it, vi } from 'vitest';
import { libraryApi } from '@src/features/library/libraryApi';
import { workbenchApi } from '@src/features/workbench/workbenchApi';

const response = value => ({ ok: true, status: 200, json: async () => value });
const versions = { items: [
  { document_id: 'new', version: 2, created_at: '2026-10-06T12:00:00Z', changes: [{ path: ['手册', '检查'], kind: 'updated' }] },
  { document_id: 'old', version: 1, created_at: '2026-10-05T12:00:00Z', changes: [] },
] };
afterEach(() => { cleanup(); vi.unstubAllGlobals(); });

it('uses the scoped versions getter and forwards cancellation', async () => {
  const fetch = vi.fn(async () => response(versions)); vi.stubGlobal('fetch', fetch);
  const controller = new AbortController();
  expect(await libraryApi.outcomeVersions('甲 / 项目', '稿/2', { signal: controller.signal })).toEqual(versions);
  const [url, options] = fetch.mock.calls[0];
  expect(String(url)).toContain('/api/v2/library/outcomes/%E7%A8%BF%2F2/versions?');
  expect(new URL(String(url), 'http://localhost').searchParams.get('project_id')).toBe('甲 / 项目');
  expect(options.signal).toBe(controller.signal);
});

it.each([['auto', {}, undefined], ['explicit', { continue_from: 'old' }, 'old'], ['new', { continue_from: null }, null]])(
  'preserves the redo %s choice and original CAS body', async (_, options, expected) => {
    vi.stubGlobal('fetch', vi.fn(async () => response({ turn: {} })));
    await workbenchApi.redo('alpha', 'turn/1', 7, options);
    const body = JSON.parse(fetch.mock.calls[0][1].body);
    expect(body).toEqual({ project_id: 'alpha', expected_revision: 7, ...(expected !== undefined ? { continue_from: expected } : {}) });
  },
);

it('renders version rows and opens the original draft as readonly Markdown', async () => {
  const { OutcomeVersions } = await import('@src/features/workbench/OutcomeVersions');
  vi.stubGlobal('fetch', vi.fn(async url => response(String(url).includes('/versions?') ? versions : { note: { document_id: 'old', markdown: '## 合成旧稿\n\n原用户段落。' } })));
  render(<OutcomeVersions projectId="alpha" documentId="new"/>);
  fireEvent.click(await screen.findByRole('button', { name: '首版' }));
  expect(await screen.findByRole('heading', { name: '合成旧稿' })).toBeInTheDocument();
  expect(screen.queryByRole('textbox')).not.toBeInTheDocument();
  expect(screen.queryByText('v1')).not.toBeInTheDocument();
  expect(String(fetch.mock.calls.at(-1)[0])).toContain('project_id=alpha');
  expect(String(fetch.mock.calls.at(-1)[0])).toContain('from=note&id=old');
});

it('does not expose a late versions response after switching projects', async () => {
  const { OutcomeVersions } = await import('@src/features/workbench/OutcomeVersions');
  let resolve; vi.stubGlobal('fetch', vi.fn(url => String(url).includes('project_id=alpha')
    ? new Promise(done => { resolve = done; }) : Promise.resolve(response({ items: [] }))));
  const view = render(<OutcomeVersions projectId="alpha" documentId="new"/>);
  view.rerender(<OutcomeVersions projectId="beta" documentId="new"/>);
  await act(async () => { resolve(response(versions)); });
  expect(screen.queryByRole('button', { name: '首版' })).not.toBeInTheDocument();
});

it('does not restore late old text after closing its read-only view', async () => {
  const { OutcomeVersions } = await import('@src/features/workbench/OutcomeVersions');
  let resolve; vi.stubGlobal('fetch', vi.fn(url => String(url).includes('/versions?')
    ? Promise.resolve(response(versions)) : new Promise(done => { resolve = done; })));
  render(<OutcomeVersions projectId="alpha" documentId="new"/>);
  fireEvent.click(await screen.findByRole('button', { name: '首版' }));
  fireEvent.click(screen.getByRole('button', { name: '关闭上一版' }));
  await act(async () => { resolve(response({ note: { document_id: 'old', markdown: '合成迟到正文' } })); });
  expect(screen.queryByText('合成迟到正文')).not.toBeInTheDocument();
});

it('offers a real getter retry after network failure', async () => {
  const { OutcomeVersions } = await import('@src/features/workbench/OutcomeVersions');
  vi.stubGlobal('fetch', vi.fn().mockRejectedValueOnce(new Error('offline')).mockResolvedValue(response(versions)));
  render(<OutcomeVersions projectId="alpha" documentId="new"/>);
  fireEvent.click(await screen.findByRole('button', { name: '重试读取版本' }));
  expect(await screen.findByRole('button', { name: '首版' })).toBeInTheDocument();
  expect(fetch).toHaveBeenCalledTimes(2);
});

it('lists exact changed heading paths and passes them to the reading view', async () => {
  const { OutcomeChanges } = await import('@src/features/workbench/OutcomeChanges');
  const select = vi.fn(), changes = [{ path: ['手册', '检查'], kind: 'updated' }, { path: ['手册', '附录'], kind: 'added' }];
  const view = render(<OutcomeChanges changes={changes} onSelect={select}/>);
  fireEvent.click(screen.getByText('改动 2'));
  fireEvent.click(screen.getByRole('button', { name: '手册 / 附录' }));
  expect(select).toHaveBeenCalledWith(['手册', '附录']);
  expect(screen.getByLabelText('新增')).toBeInTheDocument();
  expect(screen.getByLabelText('更新')).toBeInTheDocument();
  expect(screen.getByLabelText('更新')).toHaveClass('ui-status-dot');
  expect(screen.getByLabelText('更新')).toHaveStyle({ width: '6px', height: '6px' });
  view.rerender(<OutcomeChanges changes={[]} onSelect={select}/>);
  expect(screen.queryByText(/改动/)).not.toBeInTheDocument();
});

it('hides the first receipt version and shows later versions without adding an editor', async () => {
  const { OutcomeVersion } = await import('@src/features/workbench/OutcomeChanges');
  const view = render(<OutcomeVersion version={1}/>);
  expect(view.container).toBeEmptyDOMElement();
  view.rerender(<OutcomeVersion version={3}/>);
  expect(screen.getByText('v3')).toHaveClass('ui-count');
});

it('keeps a late previous-document read outside the newly selected project', async () => {
  const { OutcomeVersions } = await import('@src/features/workbench/OutcomeVersions');
  let resolve; vi.stubGlobal('fetch', vi.fn(url => String(url).includes('/versions?')
    ? Promise.resolve(response(String(url).includes('project_id=alpha') ? versions : { items: [] }))
    : new Promise(done => { resolve = done; })));
  const view = render(<OutcomeVersions projectId="alpha" documentId="new"/>);
  fireEvent.click(await screen.findByRole('button', { name: '首版' }));
  view.rerender(<OutcomeVersions projectId="beta" documentId="new"/>);
  await act(async () => { resolve(response({ note: { document_id: 'old', markdown: '合成旧范围正文' } })); });
  expect(screen.queryByText('合成旧范围正文')).not.toBeInTheDocument();
  expect(screen.queryByRole('button', { name: '关闭上一版' })).not.toBeInTheDocument();
});

it('rejects a mismatched document and retries the original selected identity', async () => {
  const { OutcomeVersions } = await import('@src/features/workbench/OutcomeVersions');
  let reads = 0; vi.stubGlobal('fetch', vi.fn(async url => response(String(url).includes('/versions?') ? versions
    : { note: ++reads === 1 ? { document_id: 'other', markdown: '合成错误正文' } : { document_id: 'old', markdown: '合成正确旧稿' } })));
  render(<OutcomeVersions projectId="alpha" documentId="new"/>);
  fireEvent.click(await screen.findByRole('button', { name: '首版' }));
  fireEvent.click(await screen.findByRole('button', { name: '重试读取上一版' }));
  expect(await screen.findByText('合成正确旧稿')).toBeInTheDocument();
  expect(screen.queryByText('合成错误正文')).not.toBeInTheDocument();
  expect(reads).toBe(2);
});
