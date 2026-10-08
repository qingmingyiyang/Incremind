import { act, cleanup, fireEvent, render, screen } from '@testing-library/react';
import { afterEach, expect, it, vi } from 'vitest';
import { Workbench } from '@src/features/workbench/Workbench';

const renders = vi.hoisted(() => new Map());
vi.mock('@src/shared/ui', async importOriginal => {
  const actual = await importOriginal();
  return { ...actual, CitedAnswer: props => {
    renders.set(props.answer, (renders.get(props.answer) || 0) + 1);
    return <actual.CitedAnswer {...props}/>;
  } };
});
afterEach(() => { cleanup(); vi.unstubAllGlobals(); renders.clear(); });

it('does not render historical answers again while the current answer streams', async () => {
  localStorage.clear(); sessionStorage.clear(); let controller, animate;
  vi.stubGlobal('requestAnimationFrame', vi.fn(callback => { animate = callback; return 1; }));
  vi.stubGlobal('cancelAnimationFrame', vi.fn());
  const historical = ['旧回答一', '旧回答二'].map((answer, index) => ({ id: `old-${index}`, intent: 'ask', user_text: `旧问题${index}`,
    receipt: { ask: { answer, citations: [], layers: {}, trace: [] } } }));
  vi.stubGlobal('fetch', vi.fn(async (url, options) => {
    if (options?.method === 'POST') return { ok: true, headers: new Headers({ 'Content-Type': 'text/event-stream' }),
      body: new ReadableStream({ start(value) { controller = value; } }) };
    return { ok: true, json: async () => String(url).includes('/threads/') ? { turns: historical } : { items: [{ id: 'thread', title: '历史' }] } };
  }));
  render(<Workbench projectId="alpha"/>); await act(async () => {});
  expect(screen.getByText('旧回答一')).toBeInTheDocument();
  fireEvent.change(screen.getByRole('textbox'), { target: { value: '新问题？' } });
  fireEvent.click(screen.getByRole('button', { name: '发送' })); await act(async () => {});
  const before = historical.map(turn => renders.get(turn.receipt.ask.answer));
  const enqueue = (event, payload) => controller.enqueue(new TextEncoder().encode(`event: ${event}\ndata: ${JSON.stringify(payload)}\n\n`));
  await act(async () => {
    enqueue('started', { thread_id: 'thread', turn: { id: 'current', user_text: '新问题？' } });
    for (let index = 0; index < 100; index++) enqueue('delta', { text: '字' });
  });
  if (animate) await act(async () => animate(16));
  expect(screen.getByText('字'.repeat(100))).toBeInTheDocument();
  expect(historical.map(turn => renders.get(turn.receipt.ask.answer))).toEqual(before);
  await act(async () => { enqueue('done', { thread_id: 'thread', turn: { id: 'current', intent: 'ask', receipt: { ask: { answer: '完整回答', citations: [], layers: {}, trace: [] } } } }); controller.close(); });
  expect(screen.getByText('完整回答')).toBeInTheDocument();
});
