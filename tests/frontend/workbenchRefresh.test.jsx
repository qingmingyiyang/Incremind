import { act, cleanup, fireEvent, render, screen } from '@testing-library/react';
import { afterEach, beforeEach, expect, it, vi } from 'vitest';
import { Workbench } from '@src/features/workbench/Workbench';

let controller, frames, loadedTurns;
const prefix = '已有完整段落。\n\n';
const saved = { id: 'refresh-turn', thread_id: 'refresh-thread', intent: 'ask', user_text: '原问题',
  receipt: { ask: { answer: null, partial: prefix, interruption: 'connection', citations: [], layers: {}, trace: [] } } };
const encode = (id, event, value) => new TextEncoder().encode(`id: ${id}\nevent: ${event}\ndata: ${JSON.stringify(value)}\n\n`);
beforeEach(() => {
  localStorage.clear(); sessionStorage.clear(); loadedTurns = [saved]; frames = new Map(); let next = 0;
  vi.stubGlobal('requestAnimationFrame', vi.fn(callback => { frames.set(++next, callback); return next; }));
  vi.stubGlobal('cancelAnimationFrame', vi.fn(id => frames.delete(id)));
  vi.stubGlobal('fetch', vi.fn(async (url, options) => {
    if (String(url).includes('/stream?')) return { ok: true, headers: new Headers({ 'Content-Type': 'text/event-stream' }),
      body: new ReadableStream({ start(value) { controller = value; } }) };
    return { ok: true, json: async () => String(url).includes('/threads/')
      ? { turns: loadedTurns } : { items: [{ id: saved.thread_id, title: '原对话' }] } };
  }));
});
afterEach(() => { cleanup(); vi.unstubAllGlobals(); });
async function paint() {
  await act(async () => { const callbacks = [...frames.values()]; frames.clear(); callbacks.forEach(callback => callback(16)); });
}
it('keeps a pending refresh read alive when an older insight is confirmed', async () => {
  const insight = { id: 'older-insight', state: 'pending', text: '旧认识', revision: 1 };
  const older = { id: 'older-turn', intent: 'inspiration', user_text: '旧灵感', receipt: { inspiration: { insight } } };
  let signal;
  fetch.mockImplementation(async (url, options) => {
    if (String(url).includes('/stream?')) {
      signal = options.signal;
      return { ok: true, headers: new Headers({ 'Content-Type': 'text/event-stream' }),
        body: new ReadableStream({ start(value) { controller = value; } }) };
    }
    return { ok: true, json: async () => String(url).includes('/confirm') ? { ...insight, state: 'active', revision: 2 }
      : String(url).includes('/threads/') ? { turns: [older, saved] }
      : { items: [{ id: saved.thread_id, title: '原对话' }] } };
  });
  render(<Workbench projectId="alpha"/>); await act(async () => {});
  expect(screen.getByRole('textbox', { name: '输入' })).toBeDisabled();
  fireEvent.click(screen.getByRole('button', { name: '确认' })); await act(async () => {});
  expect(screen.getByRole('img', { name: '已确认' })).toBeInTheDocument();
  expect(fetch.mock.calls.filter(([url]) => String(url).includes('/stream?'))).toHaveLength(1);
  expect(signal.aborted).toBe(false);
  expect(screen.getByRole('textbox', { name: '输入' })).toBeDisabled();
  await act(async () => controller.enqueue(encode(1, 'delta', { text: prefix + '确认后仍在读。' })));
  await paint();
  expect(document.querySelectorAll('.ui-cited-answer')).toHaveLength(1);
  expect(document.querySelector('.ui-cited-answer').textContent).toBe(prefix + '确认后仍在读。');
  const result = { ...saved, receipt: { ask: { answer: prefix + '确认后读取完成。', citations: [], layers: {}, trace: [] } } };
  await act(async () => { controller.enqueue(encode(2, 'done', { thread_id: saved.thread_id, turn: result })); controller.close(); });
  expect(document.querySelector('.ui-cited-answer').textContent).toBe(result.receipt.ask.answer);
  expect(screen.getByRole('textbox', { name: '输入' })).not.toBeDisabled();
  expect(screen.getByRole('button', { name: '新对话' })).not.toBeDisabled();
  expect(fetch.mock.calls.filter(([url]) => String(url).includes('/stream?'))).toHaveLength(1);
  expect(fetch.mock.calls.filter(([, options]) => options?.method === 'POST')).toHaveLength(1);
  expect(fetch.mock.calls.some(([url]) => String(url).includes('/continue'))).toBe(false);
});
it('refreshes a saved partial through GET without duplicating text or posting another turn', async () => {
  render(<Workbench projectId="alpha"/>); await act(async () => {});
  const read = fetch.mock.calls.find(([url]) => String(url).includes('/stream?'));
  expect(read).toBeTruthy(); expect(read[1].method).toBe('GET');
  expect(new URL(read[0], location.href).searchParams.get('after')).toBe('0');
  expect(document.querySelector('.ui-cited-answer').textContent).toBe(prefix);
  await act(async () => {
    controller.enqueue(encode(1, 'started', { thread_id: saved.thread_id, turn: saved }));
    controller.enqueue(encode(2, 'delta', { text: prefix + '正在续读。' }));
  });
  await act(async () => { const callbacks = [...frames.values()]; frames.clear(); callbacks.forEach(callback => callback(16)); });
  expect(document.querySelectorAll('.ui-cited-answer')).toHaveLength(1);
  expect(document.querySelector('.ui-cited-answer').textContent).toBe(prefix + '正在续读。');
  await act(async () => {
    controller.enqueue(encode(3, 'done', { thread_id: saved.thread_id, turn: { ...saved,
      receipt: { ask: { answer: prefix + '正在续读。完成。', citations: [], layers: {}, trace: [] } } } }));
    controller.close();
  });
  expect(document.querySelectorAll('.ui-cited-answer')).toHaveLength(1);
  expect(document.querySelector('.ui-cited-answer').textContent).toBe(prefix + '正在续读。完成。');
  expect(screen.queryByRole('button', { name: '继续' })).not.toBeInTheDocument();
  expect(fetch.mock.calls.filter(([, options]) => options?.method === 'POST')).toHaveLength(0);
});
it('keeps an interrupted task in its original card with one text body and its completed progress', async () => {
  const task = { state: 'interrupted', title: '原任务', partial: prefix, interruption: 'connection',
    kernel_turn_id: 'kernel-task', progress: { total: 3, done: 2 },
    division: [{ assignment_id: 'worker-1', goal: '已完成步骤', deliverable: '整理稿', state: 'done', tools: [], recalled: [] }] };
  const turn = { ...saved, intent: 'do', user_text: '原任务请求', receipt: { do: task } };
  loadedTurns = [turn];
  render(<Workbench projectId="alpha"/>); await act(async () => {});
  const card = document.getElementById(`workbench-${turn.id}`);
  const progress = screen.getByRole('img', { name: '拆活 · 干活 · 汇总' });
  expect(card).toBeInTheDocument();
  expect(screen.getByText('已完成步骤')).toBeInTheDocument();
  expect(progress.querySelectorAll('[data-progress="done"]')).toHaveLength(2);
  expect(progress.querySelectorAll('[data-progress="waiting"]')).toHaveLength(1);
  expect(document.querySelectorAll('.ui-cited-answer')).toHaveLength(1);
  expect(card.querySelector('.ui-cited-answer').textContent).toBe(prefix);
  expect(screen.getByRole('button', { name: '继续' })).toBeDisabled();
  await act(async () => {
    controller.enqueue(encode(1, 'delta', { text: prefix + '汇总续读。' }));
    controller.enqueue(encode(1, 'delta', { text: prefix + '汇总续读。' }));
  });
  await paint();
  expect(document.getElementById(`workbench-${turn.id}`)).toBe(card);
  expect(document.querySelectorAll('.ui-cited-answer')).toHaveLength(1);
  expect(card.querySelector('.ui-cited-answer').textContent).toBe(prefix + '汇总续读。');
  expect(progress.querySelectorAll('[data-progress="done"]')).toHaveLength(2);
  const result = { ...turn, receipt: { do: { ...task, partial: prefix + '汇总续读。' } } };
  await act(async () => { controller.enqueue(encode(2, 'done', { thread_id: saved.thread_id, turn: result })); controller.close(); });
  expect(document.getElementById(`workbench-${turn.id}`)).toBe(card);
  expect(card.querySelector('.ui-cited-answer').textContent).toBe(result.receipt.do.partial);
  expect(screen.getByText('已完成步骤')).toBeInTheDocument();
  expect(screen.getByRole('img', { name: '拆活 · 干活 · 汇总' }).querySelectorAll('[data-progress="done"]')).toHaveLength(2);
  expect(screen.getByRole('button', { name: '继续' })).not.toBeDisabled();
  expect(screen.getByRole('textbox', { name: '输入' })).not.toBeDisabled();
  expect(fetch.mock.calls.filter(([url]) => String(url).includes('/stream?'))).toHaveLength(1);
  expect(fetch.mock.calls.filter(([, options]) => options?.method === 'POST')).toHaveLength(0);
});
it('leaves a terminal interrupted receipt operable without automatically continuing or reading in a loop', async () => {
  render(<Workbench projectId="alpha"/>); await act(async () => {});
  await act(async () => { controller.enqueue(encode(1, 'done', { thread_id: saved.thread_id, turn: saved })); controller.close(); });
  await act(async () => {});
  expect(document.querySelectorAll('.ui-cited-answer')).toHaveLength(1);
  expect(document.querySelector('.ui-cited-answer').textContent).toBe(prefix);
  expect(screen.getByRole('button', { name: '继续' })).not.toBeDisabled();
  expect(screen.getByRole('textbox', { name: '输入' })).not.toBeDisabled();
  expect(screen.getByRole('button', { name: '新对话' })).not.toBeDisabled();
  expect(fetch.mock.calls.filter(([url]) => String(url).includes('/stream?'))).toHaveLength(1);
  expect(fetch.mock.calls.filter(([, options]) => options?.method === 'POST')).toHaveLength(0);
});
it('cancels the pending GET and buffered text when leaving its project scope', async () => {
  const originalFetch = fetch.getMockImplementation();
  fetch.mockImplementation((url, options) => new URL(url, location.href).searchParams.get('project_id') === 'beta'
    ? Promise.resolve({ ok: true, json: async () => ({ items: [] }) }) : originalFetch(url, options));
  const app = render(<Workbench projectId="alpha"/>); await act(async () => {});
  const signal = fetch.mock.calls.find(([url]) => String(url).includes('/stream?'))[1].signal;
  await act(async () => controller.enqueue(encode(1, 'delta', { text: '尚未显示的旧范围片段。' })));
  expect(frames.size).toBe(1);
  app.rerender(<Workbench projectId="beta"/>);
  expect(signal.aborted).toBe(true);
  expect(frames.size).toBe(0);
  expect(document.querySelector('.ui-cited-answer')).toBeNull();
  await act(async () => {}); await paint();
  expect(screen.queryByText('尚未显示的旧范围片段。')).not.toBeInTheDocument();
  expect(screen.getByRole('textbox', { name: '输入' })).not.toBeDisabled();
  expect(fetch.mock.calls.filter(([url]) => String(url).includes('/stream?'))).toHaveLength(1);
  expect(fetch.mock.calls.filter(([, options]) => options?.method === 'POST')).toHaveLength(0);
});
it('caches a new thread at the actual POST started event before its final receipt arrives', async () => {
  fetch.mockImplementation(async (url, options) => String(url).endsWith('/turns')
    ? { ok: true, headers: new Headers({ 'Content-Type': 'text/event-stream' }),
      body: new ReadableStream({ start(value) { controller = value; } }) }
    : { ok: true, json: async () => ({ items: [] }) });
  render(<Workbench projectId="alpha"/>); await act(async () => {});
  expect(localStorage.getItem('chriptmas-v2-thread:alpha')).toBeNull();
  fireEvent.change(screen.getByRole('textbox', { name: '输入' }), { target: { value: '怎么写？' } });
  fireEvent.click(screen.getByRole('button', { name: '发送' })); await act(async () => {});
  const turn = { ...saved, id: 'started-turn', thread_id: 'started-thread', user_text: '怎么写？' };
  await act(async () => controller.enqueue(encode(1, 'started', { thread_id: turn.thread_id, turn })));
  expect(JSON.parse(localStorage.getItem('chriptmas-v2-thread:alpha'))).toBe(turn.thread_id);
  expect(localStorage.getItem('chriptmas-v2-thread:beta')).toBeNull();
  expect(screen.getByRole('textbox', { name: '输入' })).toBeDisabled();
  expect(fetch.mock.calls.filter(([, options]) => options?.method === 'POST')).toHaveLength(1);
  expect(fetch.mock.calls.some(([url]) => String(url).includes('/stream?'))).toBe(false);
  const result = { ...turn, receipt: { ask: { answer: '正式回答。', citations: [], layers: {}, trace: [] } } };
  await act(async () => { controller.enqueue(encode(2, 'done', { thread_id: turn.thread_id, turn: result })); controller.close(); });
  expect(screen.getByText('正式回答。')).toBeInTheDocument();
  expect(screen.getByRole('textbox', { name: '输入' })).not.toBeDisabled();
  expect(JSON.parse(localStorage.getItem('chriptmas-v2-thread:alpha'))).toBe(turn.thread_id);
  expect(fetch.mock.calls.filter(([, options]) => options?.method === 'POST')).toHaveLength(1);
  expect(fetch.mock.calls.some(([url]) => String(url).includes('/stream?'))).toBe(false);
});
