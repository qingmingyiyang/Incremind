import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react';
import { afterEach, expect, it, vi } from 'vitest';
import { ContextPanel } from '@src/features/workbench/ContextPanel';

afterEach(() => { cleanup(); vi.unstubAllGlobals(); });
const entry = { id: 'method-1', layer: 'insight', title: '留出换货余地', tokens: 32,
  supplemented: true, object_revision: 2 };
const receipt = { answer: '原回答', context: { entries: [entry],
  feedback: { turn_id: 'turn-one', project_id: 'alpha' } } };
const ok = value => ({ ok: true, status: 200, headers: new Headers({ 'Content-Type': 'application/json' }), json: async () => value });

it.each(['ask', 'do'])('marks methods and persists a strike without mutating the %s receipt', async kind => {
  const before = JSON.stringify(receipt);
  const fetch = vi.fn().mockResolvedValueOnce(ok({ items: [] })).mockResolvedValue(ok({ id: 'strike-one', revision: 1 }));
  vi.stubGlobal('fetch', fetch);
  render(<ContextPanel kind={kind} receipt={receipt}/>);
  await waitFor(() => expect(fetch).toHaveBeenCalledTimes(1));
  fireEvent.click(screen.getByText('条目'));
  expect(screen.getByText('补')).toHaveClass('context-item-layer');
  fireEvent.click(screen.getByRole('button', { name: '划掉 留出换货余地' }));
  await waitFor(() => expect(screen.getByRole('button', { name: '划掉 留出换货余地' })).toBeDisabled());
  expect(screen.getByText('留出换货余地').closest('.ui-row')).toHaveClass('context-item-struck');
  expect(JSON.parse(fetch.mock.calls[1][1].body)).toEqual({ project_id: 'alpha', object_kind: 'recognition',
    object_id: 'method-1', object_revision: 2, expected_revision: 0 });
  expect(JSON.stringify(receipt)).toBe(before);
});

it('restores a persisted strike when reopening context', async () => {
  vi.stubGlobal('fetch', vi.fn().mockResolvedValue(ok({ items: [{ object_id: entry.id }] })));
  render(<ContextPanel receipt={receipt}/>);
  fireEvent.click(screen.getByText('条目'));
  await waitFor(() => expect(screen.getByRole('button', { name: '划掉 留出换货余地' })).toBeDisabled());
});

it('ignores a late strike after changing the turn', async () => {
  let finish;
  vi.stubGlobal('fetch', vi.fn((url, options) => options.method
    ? new Promise(resolve => { finish = resolve; }) : Promise.resolve(ok({ items: [] }))));
  const view = render(<ContextPanel receipt={receipt}/>);
  fireEvent.click(screen.getByText('条目'));
  fireEvent.click(screen.getByRole('button', { name: '划掉 留出换货余地' }));
  const next = { context: { entries: [{ ...entry, id: 'next-method' }], feedback: { turn_id: 'turn-two', project_id: 'beta' } } };
  view.rerender(<ContextPanel receipt={next}/>);
  finish(ok({ id: 'strike-old', revision: 1 }));
  await waitFor(() => expect(screen.getByRole('button', { name: '划掉 留出换货余地' })).not.toBeDisabled());
  expect(screen.getByText('留出换货余地').closest('.ui-row')).not.toHaveClass('context-item-struck');
});
