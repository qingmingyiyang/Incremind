import { act, cleanup, fireEvent, render, screen } from '@testing-library/react';
import { afterEach, beforeEach, expect, it, vi } from 'vitest';
import { Workbench } from '@src/features/workbench/Workbench';

let controller, signal, animationFrames;
const encode = (event, value) => new TextEncoder().encode(`event: ${event}\ndata: ${JSON.stringify(value)}\n\n`);
beforeEach(() => {
  localStorage.clear(); sessionStorage.clear();
  animationFrames = new Map(); let nextFrame = 0;
  vi.stubGlobal('requestAnimationFrame', vi.fn(callback => { animationFrames.set(++nextFrame, callback); return nextFrame; }));
  vi.stubGlobal('cancelAnimationFrame', vi.fn(id => animationFrames.delete(id)));
  vi.stubGlobal('fetch', vi.fn(async (url, options) => {
    if (String(url).endsWith('/turns')) {
      signal = options.signal;
      return { ok: true, headers: new Headers({ 'Content-Type': 'text/event-stream' }),
        body: new ReadableStream({ start(value) { controller = value; } }) };
    }
    return { ok: true, json: async () => ({ items: [] }) };
  }));
});
afterEach(() => { cleanup(); vi.unstubAllGlobals(); });
async function send() {
  fireEvent.change(screen.getByRole('textbox'), { target: { value: '怎么写？' } });
  fireEvent.click(screen.getByRole('button', { name: '发送' })); await act(async () => {});
}
async function paint() {
  await act(async () => { const callbacks = [...animationFrames.values()]; animationFrames.clear(); callbacks.forEach(callback => callback(16)); });
}
it('shows answer prefixes before completion and replaces them with the validated receipt', async () => {
  render(<Workbench projectId="alpha"/>); await act(async () => {}); await send();
  await act(async () => {
    controller.enqueue(encode('started', { thread_id: 'thread-1', turn: { id: 'turn-1', user_text: '怎么写？' } }));
    controller.enqueue(encode('delta', { text: '先写数字。' }));
  });
  await paint();
  expect(screen.getByText('先写数字。')).toBeInTheDocument();
  expect(document.querySelector('[aria-busy="true"]')).toBeTruthy();
  expect(screen.queryByLabelText(/引用/)).not.toBeInTheDocument();
  await act(async () => {
    controller.enqueue(encode('done', { thread_id: 'thread-1', turn: { id: 'turn-1', intent: 'ask', user_text: '怎么写？',
      receipt: { ask: { answer: '先写数字。再补依据。', citations: [], layers: { insight: 0, summary: 0, note: 0, source: 0, persona: 0 }, trace: [] } } } }));
    controller.close();
  });
  expect(screen.getByText('先写数字。再补依据。')).toBeInTheDocument();
  expect(screen.queryByText('先写数字。')).not.toBeInTheDocument();
});
it('aborts delivery on scope change and hides partial text immediately', async () => {
  const app = render(<Workbench projectId="alpha"/>); await act(async () => {}); await send();
  await act(async () => {
    controller.enqueue(encode('started', { thread_id: 'thread-1', turn: { id: 'turn-1', user_text: '怎么写？' } }));
    controller.enqueue(encode('delta', { text: '旧项目片段' }));
  });
  await paint();
  expect(screen.getByText('旧项目片段')).toBeInTheDocument();
  app.rerender(<Workbench projectId="beta"/>);
  expect(signal.aborted).toBe(true);
  expect(screen.queryByText('旧项目片段')).not.toBeInTheDocument();
});

it('replaces streamed text at a committed reset before displaying the new segment', async () => {
  render(<Workbench projectId="alpha"/>); await act(async () => {}); await send();
  await act(async () => {
    controller.enqueue(encode('started', { thread_id: 'thread-1', turn: { id: 'turn-1', intent: 'ask', user_text: '怎么写？' } }));
    controller.enqueue(encode('delta', { text: '旧段和未完成尾巴' }));
  }); await paint();
  expect(screen.getByText('旧段和未完成尾巴')).toBeInTheDocument();
  await act(async () => {
    controller.enqueue(encode('reset', { text: '保留的完整段落。\n\n' }));
    controller.enqueue(encode('delta', { text: '新段落。' }));
  }); await paint();
  expect(screen.queryByText('旧段和未完成尾巴')).not.toBeInTheDocument();
  expect(document.querySelector('[aria-busy="true"] .ui-cited-answer').textContent).toBe('保留的完整段落。\n\n新段落。');
});

it('shows retry countdown using the server-relative interval and clears it on real text', async () => {
  vi.useFakeTimers({ toFake: ['setTimeout', 'clearTimeout', 'setInterval', 'clearInterval', 'Date', 'performance'] });
  try {
    render(<Workbench projectId="alpha"/>); await act(async () => {}); await send();
    await act(async () => {
      controller.enqueue(encode('started', { thread_id: 'thread-1', turn: { id: 'turn-1', intent: 'ask', user_text: '怎么写？' } }));
      controller.enqueue(encode('status', { state: 'retrying', server_time: 100000,
        retry: { attempt: 3, used: 2, limit: 10, budget: 'before_output', reason: 'server', delay: 8, retry_at: 100008 } }));
    });
    expect(screen.getByText('重试 2/10 · 8s')).toHaveAttribute('title', '服务商过载');
    await act(async () => { await vi.advanceTimersByTimeAsync(2000); });
    expect(screen.getByText('重试 2/10 · 6s')).toBeInTheDocument();
    await act(async () => { controller.enqueue(encode('delta', { text: '已经恢复。' })); }); await paint();
    expect(screen.queryByText(/重试 2\/10/)).not.toBeInTheDocument();
  } finally { vi.useRealTimers(); }
});
