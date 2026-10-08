import { act, cleanup, fireEvent, render, screen } from '@testing-library/react';
import { afterEach, expect, it, vi } from 'vitest';
import { Library } from '@src/features/library/Library';

afterEach(() => { cleanup(); vi.unstubAllGlobals(); vi.useRealTimers(); });
const settle = async () => act(async () => {});

function transport({ limit = false, conflict = false } = {}) {
  return vi.fn(async (url, options = {}) => {
    const path = new URL(String(url), 'http://localhost');
    if (path.pathname.endsWith('/consolidate')) {
      if (options.method === 'POST') return { ok: !conflict, status: conflict ? 409 : 200,
        json: async () => conflict ? { detail: 'consolidate_limit' } : { job_id: 'consolidation-one' } };
      return { ok: true, json: async () => ({ score: 7, limit, running: false, job_id: null }) };
    }
    return { ok: true, json: async () => ({ items: [], counts: {} }) };
  });
}

it('shows the real correction count and starts exactly the current project', async () => {
  vi.stubGlobal('fetch', transport());
  render(<Library projectId="alpha"/>); await settle();
  fireEvent.click(screen.getByRole('button', { name: '资料库更多' }));
  const button = screen.getByRole('menuitem', { name: '现在整理 7/10' });
  expect(button).toHaveAttribute('title', '满10次自动整理');
  fireEvent.click(button); await settle();
  const calls = fetch.mock.calls.filter(([url, options]) => String(url).includes('/consolidate') && options?.method === 'POST');
  expect(calls).toHaveLength(1);
  expect(JSON.parse(calls[0][1].body)).toEqual({ project_id: 'alpha' });
});

it('disables a spent shared budget without making a request', async () => {
  vi.stubGlobal('fetch', transport({ limit: true }));
  render(<Library projectId="alpha"/>); await settle();
  fireEvent.click(screen.getByRole('button', { name: '资料库更多' }));
  const button = screen.getByRole('menuitem', { name: '现在整理 7/10' });
  expect(button).toBeDisabled();
  fireEvent.click(button); await settle();
  expect(fetch.mock.calls.filter(([, options]) => options?.method === 'POST')).toHaveLength(0);
});

it('turns a real consolidate_limit conflict into the same disabled state', async () => {
  vi.stubGlobal('fetch', transport({ conflict: true }));
  render(<Library projectId="alpha"/>); await settle();
  fireEvent.click(screen.getByRole('button', { name: '资料库更多' }));
  fireEvent.click(screen.getByRole('menuitem', { name: '现在整理 7/10' })); await settle();
  expect(screen.getByRole('menuitem', { name: '现在整理 7/10' })).toBeDisabled();
});

it('finishes the current project job after its search changes', async () => {
  let finish;
  const base = transport();
  vi.stubGlobal('fetch', vi.fn((url, options) => options?.method === 'POST'
    ? new Promise(resolve => { finish = () => resolve({ ok: true, json: async () => ({ job_id: 'consolidation-one' }) }); })
    : base(url, options)));
  render(<Library projectId="alpha"/>); await settle();
  fireEvent.click(screen.getByRole('button', { name: '资料库更多' }));
  fireEvent.click(screen.getByRole('menuitem', { name: '现在整理 7/10' }));
  expect(screen.getByRole('menuitem', { name: '现在整理 7/10' })).toBeDisabled();
  fireEvent.change(screen.getByRole('searchbox', { name: '搜索' }), { target: { value: '礼物' } });
  await act(async () => { finish(); });
  expect(screen.queryByRole('menuitem', { name: '现在整理 7/10' })).not.toBeInTheDocument();
  fireEvent.click(screen.getByRole('button', { name: '资料库更多' })); await settle();
  expect(screen.getByRole('menuitem', { name: '现在整理 7/10' })).toBeEnabled();
});

it('ignores a previous project job result after switching projects', async () => {
  let finish;
  const base = transport();
  vi.stubGlobal('fetch', vi.fn((url, options) => options?.method === 'POST'
    ? new Promise(resolve => { finish = () => resolve({ ok: false, status: 409, json: async () => ({ detail: 'consolidate_limit' }) }); })
    : base(url, options)));
  const page = render(<Library projectId="alpha"/>); await settle();
  fireEvent.click(screen.getByRole('button', { name: '资料库更多' }));
  fireEvent.click(screen.getByRole('menuitem', { name: '现在整理 7/10' }));
  page.rerender(<Library projectId="beta"/>); await settle();
  await act(async () => { finish(); });
  fireEvent.click(screen.getByRole('button', { name: '资料库更多' }));
  expect(screen.getByRole('menuitem', { name: '现在整理 7/10' })).toBeEnabled();
});

const statusResponse = value => ({ ok: true, json: async () => value });
const isStatus = (url, options) => String(url).includes('/consolidate') && options?.method !== 'POST';
const statusCalls = () => fetch.mock.calls.filter(([url, options]) => isStatus(url, options));
const advance = milliseconds => act(async () => { await vi.advanceTimersByTimeAsync(milliseconds); });

function changingStatus(initial) {
  let current = initial;
  const base = transport();
  const wire = vi.fn((url, options) => isStatus(url, options) ? Promise.resolve(statusResponse(current)) : base(url, options));
  return { wire, update: value => { current = value; } };
}

it('updates a running job to zero and the spent budget without reopening the menu, then stops reading', async () => {
  vi.useFakeTimers();
  const server = changingStatus({ score: 7, limit: false, running: true, job_id: 'consolidation-one' });
  vi.stubGlobal('fetch', server.wire);
  render(<Library projectId="alpha"/>); await settle();
  fireEvent.click(screen.getByRole('button', { name: '资料库更多' })); await settle();
  expect(screen.getByRole('menuitem', { name: '现在整理 7/10' })).toBeEnabled();
  server.update({ score: 0, limit: true, running: false, job_id: null });
  await advance(1500);
  expect(screen.getByRole('menuitem', { name: '现在整理 0/10' })).toBeDisabled();
  const completedReads = statusCalls().length;
  await advance(60000);
  expect(statusCalls()).toHaveLength(completedReads);
  expect(vi.getTimerCount()).toBe(0);
});

it('reads the shared budget again when the menu opens after another project spends it', async () => {
  const server = changingStatus({ score: 7, limit: false, running: false, job_id: null });
  vi.stubGlobal('fetch', server.wire);
  render(<Library projectId="alpha"/>); await settle();
  fireEvent.click(screen.getByRole('button', { name: '资料库更多' })); await settle();
  expect(screen.getByRole('menuitem', { name: '现在整理 7/10' })).toBeEnabled();
  fireEvent.click(screen.getByRole('button', { name: '资料库更多' }));
  const before = statusCalls().length;
  server.update({ score: 7, limit: true, running: false, job_id: null });
  fireEvent.click(screen.getByRole('button', { name: '资料库更多' })); await settle();
  expect(statusCalls()).toHaveLength(before + 1);
  expect(screen.getByRole('menuitem', { name: '现在整理 7/10' })).toBeDisabled();
});

it('keeps repeated manual requests available for the existing running project job', async () => {
  vi.useFakeTimers();
  const server = changingStatus({ score: 7, limit: false, running: true, job_id: 'consolidation-one' });
  vi.stubGlobal('fetch', server.wire);
  render(<Library projectId="alpha"/>); await settle();
  for (let repeat = 0; repeat < 2; repeat += 1) {
    fireEvent.click(screen.getByRole('button', { name: '资料库更多' })); await settle();
    const button = screen.getByRole('menuitem', { name: '现在整理 7/10' });
    expect(button).toBeEnabled();
    fireEvent.click(button); await settle();
  }
  const posts = fetch.mock.calls.filter(([, options]) => options?.method === 'POST');
  expect(posts).toHaveLength(2);
  expect(posts.map(([, options]) => JSON.parse(options.body))).toEqual([{ project_id: 'alpha' }, { project_id: 'alpha' }]);
  const replies = await Promise.all(fetch.mock.results.filter((result, index) => fetch.mock.calls[index][1]?.method === 'POST')
    .map(async result => (await result.value).json()));
  expect(replies).toEqual([{ job_id: 'consolidation-one' }, { job_id: 'consolidation-one' }]);
});

it('cancels an in-flight running status read on project change and ignores its late completion', async () => {
  vi.useFakeTimers();
  let finish, pendingSignal, alphaReads = 0;
  const base = transport();
  vi.stubGlobal('fetch', vi.fn((url, options = {}) => {
    if (!isStatus(url, options)) return base(url, options);
    const project = new URL(String(url), 'http://localhost').searchParams.get('project_id');
    if (project === 'beta') return Promise.resolve(statusResponse({ score: 2, limit: false, running: false, job_id: null }));
    alphaReads += 1;
    if (alphaReads === 1) return Promise.resolve(statusResponse({ score: 7, limit: false, running: true, job_id: 'consolidation-one' }));
    pendingSignal = options.signal;
    return new Promise(resolve => { finish = value => resolve(statusResponse(value)); });
  }));
  const page = render(<Library projectId="alpha"/>); await settle();
  await advance(1500);
  expect(alphaReads).toBe(2);
  page.rerender(<Library projectId="beta"/>); await settle();
  expect(pendingSignal.aborted).toBe(true);
  fireEvent.click(screen.getByRole('button', { name: '资料库更多' })); await settle();
  await act(async () => { finish({ score: 0, limit: true, running: true, job_id: 'consolidation-one' }); });
  expect(screen.getByRole('menuitem', { name: '现在整理 2/10' })).toBeEnabled();
  await advance(60000);
  expect(alphaReads).toBe(2);
  expect(vi.getTimerCount()).toBe(0);
});

it.each(['scheduled', 'in-flight'])('cancels the %s status work on unmount and never restarts it from a late response', async phase => {
  vi.useFakeTimers();
  let finish, pendingSignal, reads = 0;
  const base = transport();
  vi.stubGlobal('fetch', vi.fn((url, options = {}) => {
    if (!isStatus(url, options)) return base(url, options);
    reads += 1;
    pendingSignal = options.signal;
    if (reads === 1) return Promise.resolve(statusResponse({ score: 7, limit: false, running: true, job_id: 'consolidation-one' }));
    return new Promise(resolve => { finish = () => resolve(statusResponse({ score: 7, limit: false, running: true, job_id: 'consolidation-one' })); });
  }));
  const page = render(<Library projectId="alpha"/>); await settle();
  expect(vi.getTimerCount()).toBe(1);
  if (phase === 'in-flight') { await advance(1500); expect(reads).toBe(2); }
  page.unmount();
  expect(pendingSignal.aborted).toBe(true);
  expect(vi.getTimerCount()).toBe(0);
  if (finish) await act(async () => { finish(); });
  await advance(60000);
  expect(reads).toBe(phase === 'scheduled' ? 1 : 2);
  expect(vi.getTimerCount()).toBe(0);
});

it('does not apply a late job conflict after switching away and back to the same project', async () => {
  let finish;
  const base = transport();
  vi.stubGlobal('fetch', vi.fn((url, options) => options?.method === 'POST'
    ? new Promise(resolve => { finish = () => resolve({ ok: false, status: 409, json: async () => ({ detail: 'consolidate_limit' }) }); })
    : base(url, options)));
  const page = render(<Library projectId="alpha"/>); await settle();
  fireEvent.click(screen.getByRole('button', { name: '资料库更多' })); await settle();
  fireEvent.click(screen.getByRole('menuitem', { name: '现在整理 7/10' }));
  page.rerender(<Library projectId="beta"/>); await settle();
  page.rerender(<Library projectId="alpha"/>); await settle();
  fireEvent.click(screen.getByRole('button', { name: '资料库更多' })); await settle();
  await act(async () => { finish(); });
  expect(screen.getByRole('menuitem', { name: '现在整理 7/10' })).toBeEnabled();
});

it('keeps an in-flight manual request disabled when the menu revalidates', async () => {
  let finish;
  const base = transport();
  vi.stubGlobal('fetch', vi.fn((url, options) => options?.method === 'POST'
    ? new Promise(resolve => { finish = () => resolve(statusResponse({ job_id: 'consolidation-one' })); })
    : base(url, options)));
  render(<Library projectId="alpha"/>); await settle();
  fireEvent.click(screen.getByRole('button', { name: '资料库更多' })); await settle();
  fireEvent.click(screen.getByRole('menuitem', { name: '现在整理 7/10' }));
  fireEvent.click(screen.getByRole('button', { name: '资料库更多' }));
  fireEvent.click(screen.getByRole('button', { name: '资料库更多' })); await settle();
  expect(screen.getByRole('menuitem', { name: '现在整理 7/10' })).toBeDisabled();
  fireEvent.click(screen.getByRole('menuitem', { name: '现在整理 7/10' }));
  expect(fetch.mock.calls.filter(([, options]) => options?.method === 'POST')).toHaveLength(1);
  await act(async () => { finish(); });
});

it.each([2, 3])('bounds running status retries after %i failed reads and can revalidate on menu open', async failures => {
  vi.useFakeTimers();
  let reads = 0, recovered = false;
  const base = transport();
  vi.stubGlobal('fetch', vi.fn((url, options) => {
    if (!isStatus(url, options)) return base(url, options);
    reads += 1;
    if (recovered || reads > failures + 1) return Promise.resolve(statusResponse({ score: 0, limit: true, running: false, job_id: null }));
    if (reads > 1) return Promise.reject(new Error('synthetic network interruption'));
    return Promise.resolve(statusResponse({ score: 7, limit: false, running: true, job_id: 'consolidation-one' }));
  }));
  render(<Library projectId="alpha"/>); await settle();
  await advance(6000);
  expect(reads).toBe(4);
  expect(vi.getTimerCount()).toBe(0);
  await advance(60000);
  expect(reads).toBe(4);
  recovered = true;
  fireEvent.click(screen.getByRole('button', { name: '资料库更多' })); await settle();
  expect(reads).toBe(5);
  expect(screen.getByRole('menuitem', { name: '现在整理 0/10' })).toBeDisabled();
});

it('does not let a stale status read undo a newer manual budget conflict', async () => {
  let finishRead, reads = 0;
  const base = transport({ conflict: true });
  vi.stubGlobal('fetch', vi.fn((url, options) => {
    if (isStatus(url, options) && ++reads === 2) return new Promise(resolve => {
      finishRead = () => resolve(statusResponse({ score: 7, limit: false, running: false, job_id: null }));
    });
    return base(url, options);
  }));
  render(<Library projectId="alpha"/>); await settle();
  fireEvent.click(screen.getByRole('button', { name: '资料库更多' })); await settle();
  expect(reads).toBe(2);
  fireEvent.click(screen.getByRole('menuitem', { name: '现在整理 7/10' })); await settle();
  expect(screen.getByRole('menuitem', { name: '现在整理 7/10' })).toBeDisabled();
  await act(async () => { finishRead(); });
  expect(screen.getByRole('menuitem', { name: '现在整理 7/10' })).toBeDisabled();
});
