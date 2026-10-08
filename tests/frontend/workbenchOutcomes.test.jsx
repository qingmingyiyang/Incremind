import { act, cleanup, fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import { afterEach, beforeEach, expect, it, vi } from 'vitest';
import { Workbench } from '@src/features/workbench/Workbench';

const old = '# 合成手册\n\n## 检查\n\n合成上一版内容。\n\n## 附录\n\n附录旧文。';
const current = '# 合成手册\n\n## 检查\n\n合成新版内容。\n\n## 附录\n\n附录新文。';
const document = { document_id: 'new', revision: 3, title: '合成手册', markdown: current };
const task = { title: '合成手册', state: 'done', kernel_turn_id: 'kernel-1', document_id: 'new',
  progress: { done: 3, total: 3 }, division: [], continues: { document_id: 'old', version: 3 },
  changes: [{ path: ['合成手册', '检查'], kind: 'updated' }], fallback_new: false };
let turn;
const reply = (value, status = 200) => ({ ok: status < 400, status, json: async () => value });
const props = { projectId: 'alpha', projects: [{ id: 'alpha', name: '甲' }, { id: 'beta', name: '乙' }] };
beforeEach(() => {
  localStorage.clear(); turn = { id: 'do-1', intent: 'do', thread_id: 'thread-1', user_text: '帮我写合成手册', receipt: { do: { ...task } } };
  vi.stubGlobal('fetch', vi.fn(async (url, options = {}) => {
    const path = String(url);
    if (path.includes('/documents/')) return reply(document);
    if (path.includes('/drill?')) return reply({ note: { document_id: 'old', revision: 2, markdown: old } });
    if (path.includes('/versions?')) return path.includes('/ordinary/') ? reply({ detail: 'outcome_unavailable' }, 404)
      : reply({ items: [{ document_id: path.includes('/other/') ? 'other' : 'new', version: 3, created_at: '2026-10-06', changes: [] }], previous: { document_id: 'old', revision: 2, markdown: old } });
    if (path.includes('/outcomes?')) return reply({ items: [{ document_id: 'new', title: '合成手册', version: 3 }, { document_id: 'other', title: '合成另一成果', version: 1 }] });
    if (path.includes('/notes?')) return reply({ items: [{ document_id: 'ordinary', title: '合成普通整理稿' }] });
    if (path.includes('/division')) return reply({ revision: 7, items: [] });
    if (path.includes('/redo') || path.endsWith('/turns')) return reply({ thread_id: 'thread-1', turn: { ...turn, id: 'do-2' } });
    if (path.includes('/threads/')) return reply({ turns: [turn] });
    if (path.includes('project_id=beta')) return reply({ items: [] });
    return reply({ items: [{ id: 'thread-1', title: '合成对话' }] });
  }));
});
afterEach(() => { cleanup(); vi.unstubAllGlobals(); });
async function openOutcome() {
  fireEvent.click(await screen.findByRole('button', { name: '打开成果' }));
  await screen.findByRole('heading', { name: '合成手册' });
}

it('shows the actual continued receipt version and a fallback marker without inventing v1', async () => {
  turn.receipt.do.fallback_new = true;
  const view = render(<Workbench {...props}/>);
  expect(await screen.findByText('v3')).toBeInTheDocument();
  expect(screen.getByLabelText('已新写一篇')).toBeInTheDocument();
  turn = { ...turn, receipt: { do: { ...task, continues: null } } };
  view.rerender(<Workbench {...props} projectId="beta"/>);
  await act(async () => {});
  expect(screen.queryByText('v1')).not.toBeInTheDocument();
});

it('fills the actual Composer and sends its explicit document choice as a do request', async () => {
  render(<Workbench {...props}/>); await openOutcome();
  fireEvent.click(screen.getByLabelText('成果更多'));
  fireEvent.click(screen.getByRole('button', { name: '接着写' }));
  const input = screen.getByRole('textbox', { name: '输入' });
  expect(input).toHaveValue('接着《合成手册》写：'); expect(input).toHaveFocus();
  fireEvent.change(input, { target: { value: input.value + '补充合成资料' } });
  fireEvent.click(screen.getByRole('button', { name: '发送' })); await act(async () => {});
  expect(JSON.parse(fetch.mock.calls.find(([url]) => String(url).endsWith('/turns'))[1].body)).toMatchObject({ intent: 'do', continue_from: 'new', project_id: 'alpha' });
});

it('opens an ordinary document without waiting for its absent outcome chain and preserves editing', async () => {
  const original = fetch;
  let finish;
  vi.stubGlobal('fetch', vi.fn((url, options) => String(url).includes('/versions?')
    ? new Promise(resolve => { finish = resolve; }) : original(url, options)));
  render(<Workbench {...props}/>); await openOutcome();
  expect(screen.getByRole('button', { name: '保存' })).toBeInTheDocument();
  fireEvent.click(screen.getByLabelText('成果更多'));
  expect(screen.queryByRole('button', { name: '接着写' })).not.toBeInTheDocument();
  await act(async () => { finish(reply({ detail: 'outcome_unavailable' }, 404)); });
  expect(screen.queryByRole('button', { name: '接着写' })).not.toBeInTheDocument();
  expect(screen.getByRole('heading', { name: '合成手册' })).toBeInTheDocument();
});

it('does not grant continuation from an old project response after reopening the document', async () => {
  const original = fetch, pending = [];
  vi.stubGlobal('fetch', vi.fn((url, options) => String(url).includes('/versions?')
    ? new Promise(resolve => { pending.push(resolve); }) : original(url, options)));
  const view = render(<Workbench {...props}/>); await openOutcome();
  view.rerender(<Workbench {...props} projectId="beta"/>); await act(async () => {});
  view.rerender(<Workbench {...props}/>); await openOutcome();
  fireEvent.click(screen.getByLabelText('成果更多'));
  await act(async () => { pending[0](reply({ items: [{ document_id: 'new', version: 3 }] })); });
  expect(screen.queryByRole('button', { name: '接着写' })).not.toBeInTheDocument();
  await act(async () => { pending[1](reply({ items: [{ document_id: 'new', version: 3 }] })); });
  expect(screen.getByRole('button', { name: '接着写' })).toBeInTheDocument();
});

it('reads the original task division revision before explicitly redoing as a new draft', async () => {
  render(<Workbench {...props}/>);
  fireEvent.click(await screen.findByLabelText('成果选择'));
  fireEvent.click(screen.getByRole('button', { name: '新写一篇' })); await act(async () => {});
  const [url, options] = fetch.mock.calls.find(([url]) => String(url).includes('/redo'));
  expect(String(url)).toContain('/turns/do-1/redo');
  expect(JSON.parse(options.body)).toEqual({ project_id: 'alpha', expected_revision: 7, continue_from: null });
});

it('offers only documents proven by the outcome getter when switching the redo choice', async () => {
  render(<Workbench {...props}/>);
  fireEvent.click(await screen.findByLabelText('成果选择'));
  fireEvent.click(screen.getByRole('button', { name: '换一篇' }));
  fireEvent.click(await screen.findByRole('button', { name: '合成另一成果' })); await act(async () => {});
  expect(screen.queryByRole('button', { name: '合成普通整理稿' })).not.toBeInTheDocument();
  expect(JSON.parse(fetch.mock.calls.find(([url]) => String(url).includes('/redo'))[1].body)).toEqual({ project_id: 'alpha', expected_revision: 7, continue_from: 'other' });
});

it('keeps the old choice panel closed when a same-scope list arrives after a successful new-draft redo', async () => {
  const original = fetch;
  let finish;
  vi.stubGlobal('fetch', vi.fn((url, options) => String(url).includes('/outcomes?')
    ? new Promise(resolve => { finish = resolve; }) : original(url, options)));
  render(<Workbench {...props}/>);
  fireEvent.click(await screen.findByLabelText('成果选择'));
  fireEvent.click(screen.getByRole('button', { name: '换一篇' }));
  fireEvent.click(screen.getByRole('button', { name: '新写一篇' }));
  await act(async () => {});
  expect(fetch.mock.calls.filter(([url]) => String(url).includes('/redo'))).toHaveLength(1);
  await waitFor(() => expect(screen.getAllByLabelText('成果选择')).toHaveLength(2));
  await act(async () => { finish(reply({ items: [{ document_id: 'other', title: '合成另一成果', version: 1 }] })); });
  expect(screen.queryByRole('button', { name: '合成另一成果' })).not.toBeInTheDocument();
});

it('clears an explicit continuation on project switch instead of sending it in another scope', async () => {
  const view = render(<Workbench {...props}/>); await openOutcome();
  fireEvent.click(screen.getByLabelText('成果更多')); fireEvent.click(screen.getByRole('button', { name: '接着写' }));
  view.rerender(<Workbench {...props} projectId="beta"/>); await act(async () => {});
  fireEvent.change(screen.getByRole('textbox', { name: '输入' }), { target: { value: '帮我写合成新稿' } });
  fireEvent.click(screen.getByRole('button', { name: '发送' })); await act(async () => {});
  const body = JSON.parse(fetch.mock.calls.find(([url]) => String(url).endsWith('/turns'))[1].body);
  expect(body.project_id).toBe('beta'); expect(body).not.toHaveProperty('continue_from');
});

it('locates the actual changed heading and toggles readonly previous-section content', async () => {
  render(<Workbench {...props}/>); await openOutcome();
  fireEvent.click(screen.getByText('改动 1'));
  fireEvent.click(screen.getByRole('button', { name: '合成手册 / 检查' }));
  fireEvent.click(screen.getByRole('button', { name: '对照上一版 合成手册 / 检查' }));
  const comparison = await screen.findByLabelText('上一版 合成手册 / 检查');
  expect(within(comparison).getByText('合成上一版内容。')).toBeInTheDocument();
  expect(within(comparison).queryByText('附录旧文。')).not.toBeInTheDocument();
  expect(within(comparison).queryByRole('textbox')).not.toBeInTheDocument();
  fireEvent.click(screen.getByRole('button', { name: '对照上一版 合成手册 / 检查' }));
  expect(screen.queryByLabelText('上一版 合成手册 / 检查')).not.toBeInTheDocument();
});

it('compares the frozen r2 baseline instead of a subsequently edited r3 document', async () => {
  const original = fetch;
  vi.stubGlobal('fetch', vi.fn((url, options) => String(url).includes('/drill?')
    ? Promise.resolve(reply({ note: { document_id: 'old', revision: 3, markdown: old.replace('合成上一版内容。', '合成后来修改内容。') } })) : original(url, options)));
  render(<Workbench {...props}/>); await openOutcome();
  fireEvent.click(screen.getByRole('button', { name: '对照上一版 合成手册 / 检查' }));
  await act(async () => {});
  const comparison = screen.getByLabelText('上一版 合成手册 / 检查');
  expect(within(comparison).getByText('合成上一版内容。')).toBeInTheDocument();
  expect(within(comparison).queryByText('合成后来修改内容。')).not.toBeInTheDocument();
  expect(fetch.mock.calls.filter(([url]) => String(url).includes('/drill?') && new URL(String(url), 'http://localhost').searchParams.get('id') === 'old')).toHaveLength(0);
});

it.each([null, { document_id: 'other', revision: 2, markdown: old },
  { document_id: 'old', revision: 0, markdown: old }, { document_id: 'old', revision: 2 }])(
  'refuses an unavailable or invalid frozen previous baseline %# without a current-document fallback', async previous => {
    const original = fetch;
    vi.stubGlobal('fetch', vi.fn((url, options) => String(url).includes('/versions?')
      ? Promise.resolve(reply({ items: [{ document_id: 'new', version: 3 }], previous })) : original(url, options)));
    render(<Workbench {...props}/>); await openOutcome();
    fireEvent.click(screen.getByRole('button', { name: '对照上一版 合成手册 / 检查' }));
    await act(async () => {});
    const comparison = screen.getByLabelText('上一版 合成手册 / 检查');
    expect(within(comparison).getByRole('button', { name: '读取未完成 · 重试' })).toBeInTheDocument();
    expect(fetch.mock.calls.filter(([url]) => String(url).includes('/drill?') && new URL(String(url), 'http://localhost').searchParams.get('id') === 'old')).toHaveLength(0);
  },
);

it('retries the scoped frozen-baseline getter and retains the recovered r2 comparison', async () => {
  const original = fetch;
  let reads = 0;
  vi.stubGlobal('fetch', vi.fn((url, options) => String(url).includes('/versions?')
    ? Promise.resolve(reply({ items: [{ document_id: 'new', version: 3 }], previous: ++reads === 1 ? null : { document_id: 'old', revision: 2, markdown: old } })) : original(url, options)));
  render(<Workbench {...props}/>); await openOutcome();
  fireEvent.click(screen.getByRole('button', { name: '对照上一版 合成手册 / 检查' }));
  await act(async () => {});
  fireEvent.click(screen.getByRole('button', { name: '读取未完成 · 重试' }));
  await act(async () => {});
  expect(within(screen.getByLabelText('上一版 合成手册 / 检查')).getByText('合成上一版内容。')).toBeInTheDocument();
  fireEvent.click(screen.getByRole('button', { name: '对照上一版 合成手册 / 检查' }));
  fireEvent.click(screen.getByRole('button', { name: '对照上一版 合成手册 / 检查' }));
  await act(async () => {});
  expect(within(screen.getByLabelText('上一版 合成手册 / 检查')).getByText('合成上一版内容。')).toBeInTheDocument();
  expect(reads).toBe(2);
  expect(fetch.mock.calls.filter(([url]) => String(url).includes('/drill?') && new URL(String(url), 'http://localhost').searchParams.get('id') === 'old')).toHaveLength(0);
});

it.each([
  ['added', 200, false], ['updated', 200, true], ['added', 404, true],
])('compares a %s heading without inventing an old section for HTTP %s', async (kind, status, failed) => {
  turn.receipt.do.changes = [{ path: ['合成手册', '新增'], kind }];
  const original = fetch;
  vi.stubGlobal('fetch', vi.fn((url, options) => {
    if (String(url).includes('/documents/')) return Promise.resolve(reply({ ...document, markdown: current + '\n\n## 新增\n\n合成新增内容。' }));
    if (String(url).includes('/versions?')) return Promise.resolve(reply(status === 200
      ? { items: [{ document_id: 'new', version: 3 }], previous: { document_id: 'old', revision: 2, markdown: old } } : { detail: 'not_found' }, status));
    return original(url, options);
  }));
  render(<Workbench {...props}/>); await openOutcome();
  fireEvent.click(screen.getByRole('button', { name: '对照上一版 合成手册 / 新增' }));
  await act(async () => {});
  const comparison = screen.getByLabelText('上一版 合成手册 / 新增');
  expect(within(comparison).queryByRole('textbox')).not.toBeInTheDocument();
  if (failed) expect(within(comparison).getByRole('button', { name: '读取未完成 · 重试' })).toBeInTheDocument();
  else {
    expect(comparison).toHaveTextContent('');
    expect(within(comparison).queryByRole('button')).not.toBeInTheDocument();
    fireEvent.click(screen.getByRole('button', { name: '对照上一版 合成手册 / 新增' }));
    expect(screen.queryByLabelText('上一版 合成手册 / 新增')).not.toBeInTheDocument();
  }
});
