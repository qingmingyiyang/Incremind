import { act, cleanup, fireEvent, render, screen, within } from '@testing-library/react';
import { afterEach, beforeEach, expect, it, vi } from 'vitest';
import { Workbench } from '@src/features/workbench/Workbench';

const ask = { id: 'ask-local', intent: 'ask', user_text: '原问题😀\n第二行', receipt: { ask: {
  answer: '实际回答[2]【3】和字面【99】', citations: [{ n: 2, layer: 'source', id: 'source-local', title: '普通引用', quote: '引用片段' },
    { n: 3, layer: 'insight', persona: true, id: 'profile-local', title: '画像标题', quote: '画像片段' }], layers: {}, trace: [],
  profile: '画像块', usage: { tokens: 123 }, kernel_turn_id: '内部执行编号' } } };
let turns, loadDocument, writeText;
beforeEach(() => {
  localStorage.clear(); sessionStorage.clear(); turns = [structuredClone(ask)]; loadDocument = vi.fn(async () => ({ id: 'document-local', markdown: '# 实际成果\n\n正文😀', revision: 1 }));
  writeText = vi.fn(async () => {}); Object.defineProperty(navigator, 'clipboard', { configurable: true, value: { writeText } });
  vi.stubGlobal('fetch', vi.fn(async (url, init) => {
    if (String(url).includes('/documents/')) return { ok: true, json: async () => loadDocument(url, init) };
    if (String(url).includes('/threads/')) return { ok: true, json: async () => ({ turns }) };
    if (String(url).includes('/threads?')) return { ok: true, json: async () => ({ items: [{ id: 'thread-local', title: '当前对话' }] }) };
    throw new Error(`未授权请求 ${url}`);
  }));
});
afterEach(() => { cleanup(); vi.unstubAllGlobals(); });
async function openShare(index = 0) {
  const article = document.querySelectorAll('.workbench-thread article')[index];
  fireEvent.click(within(article).getByRole('button', { name: '更多' }));
  fireEvent.click(within(article).getByRole('menuitem', { name: '分享' }));
  await act(async () => {}); return screen.getByRole('dialog', { name: '分享' });
}

it('opens the real per-round preview and copies only its current ordinary citation choice without sending requests', async () => {
  const before = structuredClone(turns); render(<Workbench projectId="alpha"/>); await act(async () => {});
  const panel = await openShare(); expect(within(panel).getByText('实际回答和字面【99】')).toBeInTheDocument();
  expect(panel).not.toHaveTextContent('画像'); expect(panel).not.toHaveTextContent('123'); expect(panel).not.toHaveTextContent('内部执行编号');
  fireEvent.click(within(panel).getByRole('button', { name: '移除引用 普通引用' }));
  const requestCount = fetch.mock.calls.length; fireEvent.click(within(panel).getByRole('button', { name: '复制文字' })); await act(async () => {});
  expect(writeText).toHaveBeenCalledTimes(1); expect(writeText.mock.calls[0][0]).toContain(ask.user_text); expect(writeText.mock.calls[0][0]).toContain('实际回答和字面【99】');
  expect(writeText.mock.calls[0][0]).not.toMatch(/画像|引用片段|普通引用|123|内部执行编号/); expect(fetch).toHaveBeenCalledTimes(requestCount);
  expect(turns).toEqual(before); fireEvent.keyDown(panel, { key: 'Escape' }); expect(screen.queryByRole('dialog')).not.toBeInTheDocument();
});
it('loads a completed task actual document before copying instead of treating the task title as the answer', async () => {
  turns = [{ id: 'task-local', intent: 'do', user_text: '写出完整方案', receipt: { do: { title: '只有任务标题', state: 'done', document_id: 'document-local' } } }];
  render(<Workbench projectId="alpha"/>); await act(async () => {}); const panel = await openShare();
  expect(loadDocument).toHaveBeenCalledTimes(1); expect(loadDocument.mock.calls[0][0]).toContain('project_id=alpha'); expect(panel).toHaveTextContent('正文😀'); expect(panel).not.toHaveTextContent('只有任务标题');
  fireEvent.click(within(panel).getByRole('button', { name: '复制文字' })); await act(async () => {}); expect(writeText.mock.calls[0][0]).toContain('# 实际成果\n\n正文😀');
});
it('shares completed remember and inspiration answers through their actual existing content', async () => {
  turns = [{ id: 'remember-local', intent: 'remember', user_text: '记住材料', receipt: { remember: { state: 'done', title: '原件标题', document_id: 'document-local', insights: [], related: [] } } },
    { id: 'inspiration-local', intent: 'inspiration', user_text: '记下灵感', receipt: { inspiration: { insight: { id: 'idea-local', text: '真实灵感', state: 'pending', revision: 1 } } } }];
  render(<Workbench projectId="alpha"/>); await act(async () => {}); let panel = await openShare(0); expect(panel).toHaveTextContent('正文😀'); fireEvent.click(within(panel).getByRole('button', { name: '关闭' }));
  panel = await openShare(1); expect(panel).toHaveTextContent('真实灵感'); expect(loadDocument).toHaveBeenCalledTimes(1);
});
it('retains a failed document read and retries it without exporting a guessed answer', async () => {
  turns = [{ id: 'task-local', intent: 'do', user_text: '写方案', receipt: { do: { title: '任务标题', state: 'done', document_id: 'document-local' } } }];
  loadDocument.mockRejectedValueOnce(new Error('断开')); render(<Workbench projectId="alpha"/>); await act(async () => {});
  let panel = await openShare(); expect(within(panel).getByRole('alert')).toHaveTextContent('读取未完成'); expect(within(panel).queryByRole('button', { name: '复制文字' })).not.toBeInTheDocument(); expect(writeText).not.toHaveBeenCalled();
  fireEvent.click(within(panel).getByRole('button', { name: '重试' })); await act(async () => {}); panel = screen.getByRole('dialog', { name: '分享' }); expect(panel).toHaveTextContent('正文😀'); expect(loadDocument).toHaveBeenCalledTimes(2);
});
it('drops a late document response after a project changes without opening or copying the old snapshot', async () => {
  turns = [{ id: 'task-local', intent: 'do', user_text: '旧项目问题', receipt: { do: { title: '旧标题', state: 'done', document_id: 'document-local' } } }];
  let finish; loadDocument.mockImplementationOnce(() => new Promise(resolve => { finish = resolve; })); const app = render(<Workbench projectId="alpha"/>); await act(async () => {});
  const article = document.querySelector('.workbench-thread article'); fireEvent.click(within(article).getByRole('button', { name: '更多' })); fireEvent.click(within(article).getByRole('menuitem', { name: '分享' })); await act(async () => {});
  expect(screen.getByRole('dialog', { name: '分享' })).toBeInTheDocument(); turns = []; app.rerender(<Workbench projectId="beta"/>);
  await act(async () => { finish({ id: 'document-local', markdown: '迟到正文', revision: 1 }); }); expect(screen.queryByRole('dialog')).not.toBeInTheDocument(); expect(screen.queryByText('迟到正文')).not.toBeInTheDocument(); expect(writeText).not.toHaveBeenCalled();
});
it('closes an open snapshot when a new conversation starts and leaves an unfinished receipt unavailable', async () => {
  turns.push({ id: 'processing-local', intent: 'remember', user_text: '正在记', receipt: { remember: { state: 'processing', title: '原件', document_id: null, insights: [], related: [] } } });
  render(<Workbench projectId="alpha"/>); await act(async () => {}); const panel = await openShare(); fireEvent.click(screen.getByRole('button', { name: '新对话' })); expect(screen.queryByRole('dialog')).not.toBeInTheDocument();
  expect(writeText).not.toHaveBeenCalled(); expect(panel.isConnected).toBe(false);
});

it('retires the share preview when another existing focus panel opens', async () => {
  turns[0].receipt.ask.layers = { source: 1 }; render(<Workbench projectId="alpha"/>); await act(async () => {}); await openShare();
  fireEvent.click(screen.getByRole('button', { name: '原件 1 · 本次用了什么' }));
  expect(screen.queryByRole('dialog', { name: '分享' })).not.toBeInTheDocument(); expect(screen.getAllByRole('dialog')).toHaveLength(1); expect(screen.getByRole('dialog', { name: '上下文' })).toBeInTheDocument();
});
it('drops a late share document read when a different focus panel replaces it in the same scope', async () => {
  turns = [{ id: 'task-local', intent: 'do', user_text: '问题', receipt: { do: { title: '任务', state: 'done', document_id: 'document-local' } } }];
  let finish; loadDocument.mockImplementationOnce(() => new Promise(resolve => { finish = resolve; })); render(<Workbench projectId="alpha"/>); await act(async () => {});
  const article = document.querySelector('.workbench-thread article'); fireEvent.click(within(article).getByRole('button', { name: '更多' })); fireEvent.click(within(article).getByRole('menuitem', { name: '分享' })); await act(async () => {});
  fireEvent.click(screen.getByRole('button', { name: '本次上下文' })); expect(screen.queryByRole('dialog', { name: '分享' })).not.toBeInTheDocument();
  await act(async () => finish({ id: 'document-local', markdown: '迟到成果', revision: 1 })); expect(screen.queryByRole('dialog', { name: '分享' })).not.toBeInTheDocument(); expect(screen.queryByText('迟到成果')).not.toBeInTheDocument();
});
it('keeps an unfinished receipt unavailable for sharing instead of exporting its title', async () => {
  turns = [{ id: 'processing-local', intent: 'remember', user_text: '正在记', receipt: { remember: { state: 'processing', title: '原件标题', document_id: null, insights: [], related: [] } } }];
  render(<Workbench projectId="alpha"/>); await act(async () => {}); fireEvent.click(screen.getByRole('button', { name: '更多' }));
  expect(screen.getByRole('menuitem', { name: '分享' })).toBeDisabled(); expect(loadDocument).not.toHaveBeenCalled(); expect(writeText).not.toHaveBeenCalled();
});
