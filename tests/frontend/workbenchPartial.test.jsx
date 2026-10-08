import { act, cleanup, fireEvent, render, screen } from '@testing-library/react';
import { afterEach, beforeEach, expect, it, vi } from 'vitest';
import { Workbench } from '@src/features/workbench/Workbench';

const partial = '保留的完整段落。\n\n';
let complete, continuationSignal;
const interrupted = reason => ({ id: 'turn-partial', intent: 'ask', user_text: '接着解释',
  receipt: { ask: { answer: null, partial, interruption: reason, citations: [], layers: {}, trace: [] } } });
beforeEach(() => { localStorage.clear(); sessionStorage.clear(); });
afterEach(() => { cleanup(); vi.unstubAllGlobals(); });
function install(reason) {
  vi.stubGlobal('fetch', vi.fn(async (url, options) => {
    if (String(url).includes('/turn-partial/continue')) {
      continuationSignal = options.signal;
      return new Promise(resolve => { complete = () => resolve({ ok: true, json: async () => ({
        ...interrupted(reason), receipt: { ask: { answer: partial + '续写完成。', citations: [], layers: {}, trace: [] } },
      }) }); });
    }
    return { ok: true, json: async () => String(url).includes('/threads/')
      ? { turns: [interrupted(reason)] } : { items: [{ id: 'thread-1', title: '已有对话' }] } };
  }));
}
for (const [reason, label] of [['connection', '连接中断'], ['sleep', '电脑休眠']]) {
  it(`retains the ${reason} partial and continues the same turn only after an explicit click`, async () => {
    install(reason);
    render(<Workbench projectId="alpha"/>); await act(async () => {});
    expect(document.querySelector('.ui-cited-answer').textContent).toBe(partial);
    expect(screen.getByText(label)).toBeInTheDocument();
    const posts = () => fetch.mock.calls.filter(([, options]) => options?.method === 'POST');
    expect(posts()).toHaveLength(0);
    const button = screen.getByRole('button', { name: '继续' });
    fireEvent.click(button); await act(async () => {});
    expect(button).toBeDisabled();
    expect(document.querySelector('.ui-cited-answer').textContent).toBe(partial);
    expect(posts()).toHaveLength(1);
    const [url, options] = posts()[0];
    expect(url).toContain('/turns/turn-partial/continue');
    expect(JSON.parse(options.body)).toEqual({ project_id: 'alpha' });
    expect(new Headers(options.headers).get('Idempotency-Key')).toBeTruthy();
    await act(async () => { complete(); });
    expect(document.querySelectorAll('.ui-cited-answer')).toHaveLength(1);
    expect(document.querySelector('.ui-cited-answer').textContent).toBe(partial + '续写完成。');
    expect(screen.queryByRole('button', { name: '继续' })).not.toBeInTheDocument();
    expect(posts()).toHaveLength(1);
  });
}
it('aborts continuation when leaving the project and ignores its late response', async () => {
  install('connection');
  const app = render(<Workbench projectId="alpha"/>); await act(async () => {});
  fireEvent.click(screen.getByRole('button', { name: '继续' })); await act(async () => {});
  app.rerender(<Workbench projectId="beta"/>); await act(async () => {});
  expect(continuationSignal.aborted).toBe(true);
  await act(async () => { complete(); });
  expect(screen.queryByText('续写完成。', { exact: false })).not.toBeInTheDocument();
  expect(fetch.mock.calls.filter(([, options]) => options?.method === 'POST')).toHaveLength(1);
});
it('shows an interrupted task summary and retains its completed progress while continuing', async () => {
  const saved = { id: 'task-partial', intent: 'do', user_text: '完成报告', receipt: { do: {
    state: 'interrupted', title: '报告', partial, interruption: 'connection', progress: { done: 2, total: 3 },
  } } };
  vi.stubGlobal('fetch', vi.fn(async (url, options) => {
    if (options?.method === 'POST') return new Promise(resolve => { complete = () => resolve({
      ok: true, json: async () => ({ ...saved, receipt: { do: { state: 'done', title: '报告', progress: { done: 3, total: 3 } } } }),
    }); });
    return { ok: true, json: async () => String(url).includes('/threads/')
      ? { turns: [saved] } : { items: [{ id: 'thread-task', title: '已有任务' }] } };
  }));
  render(<Workbench projectId="alpha"/>); await act(async () => {});
  expect(document.querySelector('.ui-cited-answer').textContent).toBe(partial);
  expect(screen.getByLabelText('干活')).toBeInTheDocument();
  const progress = () => [...screen.getByLabelText('准备 · 批准 · 成果').querySelectorAll('[data-progress]')]
    .map(dot => dot.dataset.progress);
  expect(progress()).toEqual(['done', 'done', 'waiting']);
  const button = screen.getByRole('button', { name: '继续' });
  fireEvent.click(button); await act(async () => {});
  expect(button).toBeDisabled();
  expect(document.querySelector('.ui-cited-answer').textContent).toBe(partial);
  expect(progress()).toEqual(['done', 'done', 'waiting']);
  const [url, options] = fetch.mock.calls.find(([, value]) => value?.method === 'POST');
  expect(url).toContain('/turns/task-partial/continue');
  expect(JSON.parse(options.body)).toEqual({ project_id: 'alpha' });
  await act(async () => { complete(); });
  expect(screen.queryByRole('button', { name: '继续' })).not.toBeInTheDocument();
  expect(progress()).toEqual(['done', 'done', 'done']);
  expect(fetch.mock.calls.filter(([, value]) => value?.method === 'POST')).toHaveLength(1);
});
it('retains failed task partial text without offering an unavailable continuation', async () => {
  const saved = { id: 'failed-task', intent: 'do', user_text: '完成报告', receipt: { do: {
    state: 'failed', title: '报告', partial, interruption: 'connection', progress: { done: 2, total: 3 },
  } } };
  vi.stubGlobal('fetch', vi.fn(async url => ({ ok: true, json: async () => String(url).includes('/threads/')
    ? { turns: [saved] } : { items: [{ id: 'failed-thread', title: '已有任务' }] } })));
  render(<Workbench projectId="alpha"/>); await act(async () => {});
  expect(document.querySelector('.ui-cited-answer').textContent).toBe(partial);
  expect(screen.getByText('连接中断')).toBeInTheDocument();
  expect(screen.queryByRole('button', { name: '继续' })).not.toBeInTheDocument();
  expect(fetch.mock.calls.filter(([, options]) => options?.method === 'POST')).toHaveLength(0);
});
it('aborts a continuation on a same-project thread change and preserves the new thread', async () => {
  vi.stubGlobal('fetch', vi.fn(async (url, options) => {
    if (options?.method === 'POST') {
      continuationSignal = options.signal;
      return new Promise(resolve => { complete = () => resolve({ ok: true, json: async () => ({
        ...interrupted('connection'), receipt: { ask: { answer: '旧对话晚到续写', citations: [], layers: {}, trace: [] } },
      }) }); });
    }
    return { ok: true, json: async () => String(url).includes('/threads/second')
      ? { turns: [{ id: 'second-turn', intent: 'ask', receipt: { ask: { answer: '第二对话', citations: [], layers: {}, trace: [] } } }] }
      : String(url).includes('/threads/first') ? { turns: [interrupted('connection')] }
        : { items: [{ id: 'first', title: '第一' }, { id: 'second', title: '第二' }] } };
  }));
  const app = render(<Workbench projectId="alpha" threadId="first"/>); await act(async () => {});
  fireEvent.click(screen.getByRole('button', { name: '继续' })); await act(async () => {});
  app.rerender(<Workbench projectId="alpha" threadId="second"/>); await act(async () => {});
  expect(continuationSignal.aborted).toBe(true);
  expect(screen.getByText('第二对话')).toBeInTheDocument();
  await act(async () => { complete(); });
  expect(screen.queryByText('旧对话晚到续写')).not.toBeInTheDocument();
  expect(screen.getByText('第二对话')).toBeInTheDocument();
  expect(fetch.mock.calls.filter(([, options]) => options?.method === 'POST')).toHaveLength(1);
});
