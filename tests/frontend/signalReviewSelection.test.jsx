import { act, cleanup, fireEvent, render, screen } from '@testing-library/react';
import { useState } from 'react';
import { afterEach, expect, it, vi } from 'vitest';
import { SignalReviews, useSignalReviews } from '@src/features/library/SignalReviews';

afterEach(() => { cleanup(); vi.useRealTimers(); vi.unstubAllGlobals(); });
const row = (id, revision = 1) => ({ id, revision, title: id, kind: 'reask', effect: 'correction',
  evidence: { questions: ['第一次', '第二次'], answer: '原回答', count: 3 } });
const settle = async () => act(async () => {});
function Harness({ project = 'alpha' }) {
  const [refresh, setRefresh] = useState(0);
  const reviews = useSignalReviews(project, refresh);
  return <><button onClick={() => reviews.reload()}>更新清单</button><SignalReviews projectId={project}
    reviews={reviews} onChanged={() => setRefresh(value => value + 1)}/></>;
}

it('retains a new selection while a delayed post-decision reload returns unchanged rows', async () => {
  vi.useFakeTimers();
  let reads = 0, resolveReload;
  const fetcher = vi.fn(async (url, options = {}) => {
    if (options.method === 'POST') {
      expect(JSON.parse(options.body)).toEqual({ project_id: 'alpha', items: [{ id: 'a', action: 'confirm', expected_revision: 1 }] });
      return { ok: true, json: async () => ({ items: [{ id: 'a', state: 'confirmed' }] }) };
    }
    if (++reads === 1) return { ok: true, json: async () => ({ items: [row('a'), row('b')] }) };
    return new Promise(resolve => { resolveReload = () => resolve({ ok: true, json: async () => ({ items: [row('b')] }) }); });
  });
  vi.stubGlobal('fetch', fetcher);
  render(<Harness/>); await settle();
  fireEvent.click(screen.getByRole('checkbox', { name: '选择 a' }));
  fireEvent.click(screen.getByRole('button', { name: '确认 1' })); await settle();
  await act(async () => { vi.advanceTimersByTime(200); });
  expect(screen.queryByRole('checkbox', { name: '选择 a' })).not.toBeInTheDocument();
  fireEvent.click(screen.getByRole('checkbox', { name: '选择 b' }));
  expect(screen.getByRole('checkbox', { name: '选择 b' })).toBeChecked();
  await act(async () => { resolveReload(); });
  expect(screen.getByRole('checkbox', { name: '选择 b' })).toBeChecked();
  expect(screen.getByRole('button', { name: '确认 1' })).toBeEnabled();
  expect(fetcher.mock.calls.filter(([, options]) => options?.method === 'POST')).toHaveLength(1);
});

it('drops removed and revised selections while retaining an unchanged selection', async () => {
  let items = [row('a'), row('b'), row('c')];
  vi.stubGlobal('fetch', vi.fn(async () => ({ ok: true, json: async () => ({ items: items.map(value => ({ ...value })) }) })));
  render(<Harness/>); await settle();
  for (const id of ['a', 'b', 'c']) fireEvent.click(screen.getByRole('checkbox', { name: `选择 ${id}` }));
  items = [row('b', 2), row('c')]; fireEvent.click(screen.getByRole('button', { name: '更新清单' })); await settle();
  expect(screen.queryByRole('checkbox', { name: '选择 a' })).not.toBeInTheDocument();
  expect(screen.getByRole('checkbox', { name: '选择 b' })).not.toBeChecked();
  expect(screen.getByRole('checkbox', { name: '选择 c' })).toBeChecked();
  expect(screen.getByRole('button', { name: '确认 1' })).toBeEnabled();
});

it('clears selection when the project changes even when IDs and revisions match', async () => {
  vi.stubGlobal('fetch', vi.fn(async () => ({ ok: true, json: async () => ({ items: [row('a')] }) })));
  const view = render(<Harness/>); await settle();
  fireEvent.click(screen.getByRole('checkbox', { name: '选择 a' }));
  expect(screen.getByRole('checkbox', { name: '选择 a' })).toBeChecked();
  view.rerender(<Harness project="beta"/>); await settle();
  expect(screen.getByRole('checkbox', { name: '选择 a' })).not.toBeChecked();
  expect(screen.getByRole('button', { name: '确认 0' })).toBeDisabled();
});
