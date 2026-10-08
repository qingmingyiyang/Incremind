import { act, cleanup, fireEvent, render, screen, within } from '@testing-library/react';
import { afterEach, beforeEach, expect, it, vi } from 'vitest';
import { Library } from '@src/features/library/Library';

const reask = { id: 'review-reask', kind: 'reask', title: '怎样安排阅读？', revision: 2, effect: 'correction',
  evidence: { count: 3, questions: ['怎样安排阅读？', '能按每天半小时安排吗？'], answer: '先读两章。\n记录一个问题。' } };
const unused = { id: 'review-unused', kind: 'unused', title: '阅读顺序', revision: 4, effect: 'cool',
  evidence: { sent: 6, used: 0, questions: ['最近该读哪本书？'], object: { kind: 'document', id: 'doc-one', revision: 3 } } };
const stop = { id: 'review-stop', kind: 'stop', title: '书单够了吗？', revision: 1, effect: 'correction',
  evidence: { count: 1, questions: ['书单够了吗？'], answer: '先保留三本。' } };
const insight = { id: 'recognition-one', kind: 'recognition', text: '原资料库认识', revision: 1, state: 'active', source_count: 1 };
const reply = (value, status = 200) => ({ ok: status < 400, status, json: async () => value });
const deferred = () => { let resolve, reject; const promise = new Promise((yes, no) => { resolve = yes; reject = no; }); return { promise, resolve, reject }; };
let reviews, reviewRead, decision, reads, decisions;
function wire(url, options = {}) {
  const path = new URL(String(url), 'http://localhost');
  const project = path.searchParams.get('project_id');
  if (path.pathname.endsWith('/signal-reviews/decide')) {
    decisions.push({ body: JSON.parse(options.body), options });
    if (decision) return decision(options);
    const selected = JSON.parse(options.body).items;
    reviews = reviews.filter(row => !selected.some(item => item.id === row.id));
    return Promise.resolve(reply({ items: selected.map(row => ({ id: row.id, state: row.action === 'confirm' ? 'confirmed' : 'dismissed' })) }));
  }
  if (path.pathname.endsWith('/signal-reviews')) {
    reads.push({ project, options });
    return reviewRead ? reviewRead(project, options) : Promise.resolve(reply({ items: reviews }));
  }
  if (path.pathname.endsWith('/drill')) return Promise.resolve(reply({
    note: { document_id: 'doc-one', title: '阅读顺序', markdown: '# 原整理稿\n实际依据正文', revision: 3, verified: true, facts: [] }, grown: [],
  }));
  if (path.pathname.endsWith('/consolidate')) return Promise.resolve(reply({ score: 0, running: false, limit: false }));
  if (path.pathname.endsWith('/insights')) return Promise.resolve(reply({ items: project === 'inbox' ? [] : [insight], counts: { active: 1 } }));
  if (path.pathname.endsWith('/summaries')) return Promise.resolve(reply({ items: [{ document_id: 'doc-one', title: '原摘要', summary: '摘要原文' }] }));
  return Promise.resolve(reply({ items: [] }));
}
beforeEach(() => {
  reviews = [reask, unused, stop]; reviewRead = null; decision = null; reads = []; decisions = [];
  vi.stubGlobal('fetch', vi.fn(wire));
});
afterEach(() => { cleanup(); vi.unstubAllGlobals(); vi.useRealTimers(); });
const settle = () => act(async () => {});
const mount = (props = {}) => render(<Library projectId="alpha" projects={[{ id: 'alpha', name: '阅读', scenes: ['早晨'] }]} {...props}/>);
async function enter() {
  fireEvent.click(screen.getByRole('button', { name: '资料库更多' })); await settle();
  fireEvent.click(screen.getByRole('menuitem', { name: `纠偏 ${reviews.length}` })); await settle();
}
const choose = title => fireEvent.click(screen.getByRole('checkbox', { name: `选择 ${title}` }));

it('hides zero reviews and leaves the original constraint and memo actions intact', async () => {
  reviews = []; mount(); await settle();
  fireEvent.click(screen.getByRole('button', { name: '资料库更多' })); await settle();
  expect(screen.queryByRole('menuitem', { name: /纠偏/ })).not.toBeInTheDocument();
  expect(screen.getAllByRole('menuitem').map(row => row.textContent)).toEqual(['现在整理 0/10', '约束 0', '备忘']);
  expect(screen.getByRole('button', { name: '原资料库认识' })).toBeInTheDocument();
});

it('replaces the tabs with two columns and restores the prior layer and search', async () => {
  mount(); await settle(); fireEvent.click(screen.getByRole('button', { name: '摘要 1' }));
  fireEvent.change(screen.getByRole('searchbox'), { target: { value: '阅读' } }); await settle(); await enter();
  expect(screen.queryByRole('group', { name: '资料层级' })).not.toBeInTheDocument();
  expect(screen.getByRole('heading', { name: '纠偏 3' })).toBeInTheDocument();
  expect(screen.getByText('重问 3')).toBeInTheDocument(); expect(screen.getByText('送 6 · 用 0')).toBeInTheDocument();
  expect(screen.getAllByText('+1')).toHaveLength(2); expect(screen.getByText('降权')).toBeInTheDocument();
  fireEvent.click(screen.getByRole('button', { name: reask.title }));
  const panel = screen.getByRole('dialog', { name: '纠偏' });
  expect(within(panel).getByText(reask.evidence.questions[1])).toBeInTheDocument();
  expect(panel).toHaveTextContent('先读两章。'); expect(panel).toHaveTextContent('记录一个问题。');
  expect(document.querySelector('[data-icon="review-reask"]')).toHaveAttribute('width', '14');
  fireEvent.click(screen.getByRole('button', { name: '返回资料库' }));
  expect(screen.getByRole('button', { name: '摘要 1' })).toHaveAttribute('aria-pressed', 'true');
  expect(screen.getByRole('searchbox')).toHaveValue('阅读'); expect(screen.queryByRole('dialog')).not.toBeInTheDocument();
});

it('sends exact batch revisions and fades processed rows without a toast', async () => {
  vi.useFakeTimers(); mount(); await settle(); await enter();
  expect(screen.getByRole('button', { name: '确认 0' })).toBeDisabled(); expect(screen.getByRole('button', { name: '丢弃 0' })).toBeDisabled();
  choose(reask.title); choose(unused.title); fireEvent.click(screen.getByRole('button', { name: '确认 2' })); await settle();
  expect(decisions.map(row => row.body)).toEqual([{ project_id: 'alpha', items: [
    { id: reask.id, action: 'confirm', expected_revision: 2 }, { id: unused.id, action: 'confirm', expected_revision: 4 },
  ] }]);
  expect(screen.getByRole('button', { name: reask.title }).closest('.ui-row')).toHaveClass('is-leaving');
  expect(screen.queryByRole('status')).not.toBeInTheDocument();
  await act(async () => { await vi.advanceTimersByTimeAsync(200); });
  expect(screen.queryByRole('button', { name: reask.title })).not.toBeInTheDocument();
  expect(screen.getByRole('button', { name: '确认 0' })).toBeDisabled();
  expect(screen.getByRole('heading', { name: '纠偏 1' })).toBeInTheDocument();
});

it('dismisses selected rows with the same CAS contract', async () => {
  mount(); await settle(); await enter(); choose(stop.title);
  fireEvent.click(screen.getByRole('button', { name: '丢弃 1' })); await settle();
  expect(decisions[0].body).toEqual({ project_id: 'alpha', items: [{ id: stop.id, action: 'dismiss', expected_revision: 1 }] });
  expect(screen.getByRole('button', { name: stop.title }).closest('.ui-row')).toHaveClass('is-leaving');
});

it('opens unused evidence through the real project-scoped drill owner', async () => {
  mount(); await settle(); await enter(); fireEvent.click(screen.getByRole('button', { name: unused.title }));
  const panel = screen.getByRole('dialog', { name: '纠偏' });
  expect(panel).toHaveTextContent('最近该读哪本书？');
  fireEvent.click(within(panel).getByRole('button', { name: '打开 阅读顺序' })); await settle();
  expect(screen.getByRole('dialog', { name: '整理稿' })).toHaveTextContent('实际依据正文');
  const path = new URL(String(fetch.mock.calls.find(([url]) => String(url).includes('/drill'))[0]), 'http://localhost');
  expect(Object.fromEntries(path.searchParams)).toEqual({ project_id: 'alpha', from: 'note', id: 'doc-one' });
  expect(decisions).toHaveLength(0);
});

it('keeps the library readable when review loading fails and retries only the review read', async () => {
  reviewRead = () => Promise.reject(new TypeError('network')); mount(); await settle();
  expect(screen.getByRole('button', { name: '原资料库认识' })).toBeInTheDocument();
  fireEvent.click(screen.getByRole('button', { name: '资料库更多' })); await settle();
  expect(screen.queryByRole('menuitem', { name: /纠偏 \d/ })).not.toBeInTheDocument();
  const previous = reads.length; reviewRead = null;
  fireEvent.click(screen.getByRole('button', { name: '重试纠偏' })); await settle();
  expect(reads).toHaveLength(previous + 1); expect(screen.getByRole('menuitem', { name: '纠偏 3' })).toBeInTheDocument();
});

it('rereads a 409 and clears stale selections before another explicit decision', async () => {
  mount(); await settle(); await enter(); choose(reask.title);
  decision = () => { reviews = [{ ...reask, revision: 5 }]; return reply({ detail: 'signal_reviews_changed' }, 409); };
  const previous = reads.length; fireEvent.click(screen.getByRole('button', { name: '确认 1' })); await settle();
  expect(reads).toHaveLength(previous + 1); expect(decisions).toHaveLength(1);
  expect(screen.getByRole('button', { name: '确认 0' })).toBeDisabled();
  decision = null; choose(reask.title); fireEvent.click(screen.getByRole('button', { name: '确认 1' })); await settle();
  expect(decisions[1].body.items).toEqual([{ id: reask.id, action: 'confirm', expected_revision: 5 }]);
});

it('keeps a network-failed selection for explicit retry without automatic POST', async () => {
  mount(); await settle(); await enter(); choose(reask.title); decision = () => Promise.reject(new TypeError('network'));
  fireEvent.click(screen.getByRole('button', { name: '确认 1' })); await settle();
  expect(decisions).toHaveLength(1); expect(screen.getByRole('checkbox', { name: `选择 ${reask.title}` })).toBeChecked();
  decision = null; fireEvent.click(screen.getByRole('button', { name: '确认 1' })); await settle();
  expect(decisions).toHaveLength(2); expect(decisions[1].body).toEqual(decisions[0].body);
});

it('locks a pending decision against double clicks', async () => {
  const pending = deferred(); decision = () => pending.promise;
  mount(); await settle(); await enter(); choose(reask.title);
  const button = screen.getByRole('button', { name: '确认 1' }); fireEvent.click(button); fireEvent.click(button);
  expect(decisions).toHaveLength(1); expect(button).toBeDisabled();
  await act(async () => { pending.resolve(reply({ items: [{ id: reask.id, state: 'confirmed' }] })); });
});

it('aborts old project reads and ignores them even when the transport delivers late', async () => {
  const pending = deferred(); reviewRead = project => project === 'alpha' ? pending.promise : reply({ items: [{ ...stop, title: '乙项目证据' }] });
  const page = mount(); await settle(); page.rerender(<Library projectId="beta"/>); await settle();
  expect(reads[0].options.signal.aborted).toBe(true);
  await act(async () => { pending.resolve(reply({ items: [reask] })); });
  fireEvent.click(screen.getByRole('button', { name: '资料库更多' })); await settle();
  fireEvent.click(screen.getByRole('menuitem', { name: '纠偏 1' }));
  expect(screen.getByRole('button', { name: '乙项目证据' })).toBeInTheDocument();
  expect(screen.queryByRole('button', { name: reask.title })).not.toBeInTheDocument();
});

it('aborts old decisions and never applies their error or reread to a new project', async () => {
  const pending = deferred(); decision = () => pending.promise;
  const page = mount(); await settle(); await enter(); choose(reask.title);
  fireEvent.click(screen.getByRole('button', { name: '确认 1' }));
  page.rerender(<Library projectId="beta"/>); await settle();
  const previous = reads.length; expect(decisions[0].options.signal.aborted).toBe(true);
  await act(async () => { pending.resolve(reply({ detail: 'signal_reviews_changed' }, 409)); });
  expect(reads).toHaveLength(previous); expect(screen.queryByRole('heading', { name: /纠偏/ })).not.toBeInTheDocument();
  expect(screen.queryByRole('alert')).not.toBeInTheDocument();
});

it('rejects malformed evidence without replacing the original list', async () => {
  reviews = [{ ...reask, revision: true }]; mount(); await settle();
  fireEvent.click(screen.getByRole('button', { name: '资料库更多' })); await settle();
  expect(screen.queryByRole('menuitem', { name: /纠偏 \d/ })).not.toBeInTheDocument();
  expect(screen.getByRole('button', { name: '重试纠偏' })).toBeInTheDocument();
  expect(screen.getByRole('button', { name: '原资料库认识' })).toBeInTheDocument();
});

it('refreshes the count when reopening the menu in the same project', async () => {
  mount(); await settle(); reviews = [stop];
  const previous = reads.length;
  fireEvent.click(screen.getByRole('button', { name: '资料库更多' })); await settle();
  expect(reads).toHaveLength(previous + 1);
  expect(screen.getByRole('menuitem', { name: '纠偏 1' })).toBeInTheDocument();
});

it('aborts a pending decision on return and ignores its late failure', async () => {
  const pending = deferred(); decision = () => pending.promise;
  mount(); await settle(); await enter(); choose(reask.title);
  fireEvent.click(screen.getByRole('button', { name: '确认 1' }));
  fireEvent.click(screen.getByRole('button', { name: '返回资料库' }));
  expect(decisions[0].options.signal.aborted).toBe(true);
  await act(async () => { pending.resolve(reply({}, 503)); });
  expect(screen.queryByRole('alert')).not.toBeInTheDocument();
  expect(screen.getByRole('button', { name: '原资料库认识' })).toBeInTheDocument();
  expect(decisions).toHaveLength(1);
});

it('rereads an unknown decision delivery on error retry without another POST', async () => {
  mount(); await settle(); await enter(); choose(reask.title);
  decision = () => { reviews = []; return Promise.reject(new TypeError('lost response')); };
  fireEvent.click(screen.getByRole('button', { name: '确认 1' })); await settle();
  const previous = reads.length;
  fireEvent.click(screen.getByRole('button', { name: '重试' })); await settle();
  expect(reads).toHaveLength(previous + 1); expect(decisions).toHaveLength(1);
  expect(screen.queryByRole('button', { name: reask.title })).not.toBeInTheDocument();
  expect(screen.getByRole('button', { name: '确认 0' })).toBeDisabled();
  expect(screen.queryByRole('alert')).not.toBeInTheDocument();
});

it('drills the review project from an inbox view and returns to the original inbox', async () => {
  mount(); await settle();
  fireEvent.click(screen.getByRole('button', { name: '收件箱 1' })); await settle();
  await enter(); fireEvent.click(screen.getByRole('button', { name: unused.title }));
  fireEvent.click(within(screen.getByRole('dialog')).getByRole('button', { name: '打开 阅读顺序' })); await settle();
  const path = new URL(String(fetch.mock.calls.find(([url]) => String(url).includes('/drill'))[0]), 'http://localhost');
  expect(path.searchParams.get('project_id')).toBe('alpha');
  expect(screen.getByRole('dialog', { name: '整理稿' })).toHaveTextContent('实际依据正文');
  fireEvent.click(screen.getByRole('button', { name: '返回资料库' }));
  expect(screen.getByRole('button', { name: '收件箱 1' })).toHaveAttribute('aria-pressed', 'true');
  expect(screen.getByRole('group', { name: '资料层级' })).toBeInTheDocument();
  expect(screen.queryByRole('dialog')).not.toBeInTheDocument();
});

it('shows a drill failure in the evidence panel and retries only the same project GET', async () => {
  const original = fetch, retryRead = deferred(); let failed = true, retried = false, editorProjection = false;
  vi.stubGlobal('fetch', vi.fn((url, options) => {
    if (!String(url).includes('/drill')) return original(url, options);
    if (failed) return Promise.reject(new TypeError('transport down'));
    if (!retried) { retried = true; return retryRead.promise; }
    editorProjection = Boolean(screen.queryByRole('dialog', { name: '整理稿' }));
    return original(url, options);
  }));
  mount(); await settle(); await enter(); fireEvent.click(screen.getByRole('button', { name: unused.title }));
  fireEvent.click(within(screen.getByRole('dialog')).getByRole('button', { name: '打开 阅读顺序' })); await settle();
  const panel = screen.getByRole('dialog', { name: '纠偏' });
  expect(within(panel).getByRole('alert')).toHaveTextContent('读取未完成 · 重试');
  expect(fetch.mock.calls.filter(([url]) => String(url).includes('/drill'))).toHaveLength(1);
  failed = false; fireEvent.click(within(panel).getByRole('button', { name: '重试' })); await settle();
  expect(fetch.mock.calls.filter(([url]) => String(url).includes('/drill'))).toHaveLength(2);
  expect(screen.queryByRole('dialog', { name: '整理稿' })).not.toBeInTheDocument();
  await act(async () => { retryRead.resolve(await wire('/api/v2/library/drill?project_id=alpha&from=note&id=doc-one')); });
  expect(screen.getByRole('dialog', { name: '整理稿' })).toHaveTextContent('实际依据正文');
  const calls = fetch.mock.calls.filter(([url]) => String(url).includes('/drill'));
  // Opening the actual editor also rereads its optional comment projection.
  expect(calls).toHaveLength(3);
  expect(editorProjection).toBe(true);
  expect(calls.map(([url]) => Object.fromEntries(new URL(String(url), 'http://localhost').searchParams))).toEqual([
    { project_id: 'alpha', from: 'note', id: 'doc-one' }, { project_id: 'alpha', from: 'note', id: 'doc-one' },
    { project_id: 'alpha', from: 'note', id: 'doc-one' },
  ]);
  expect(decisions).toHaveLength(0);
});

it.each(['return', 'select', 'close'])('ignores a pending drill after %s without reopening the old object', async action => {
  const pending = deferred(), original = fetch;
  vi.stubGlobal('fetch', vi.fn((url, options) => String(url).includes('/drill') ? pending.promise : original(url, options)));
  mount(); await settle(); await enter(); fireEvent.click(screen.getByRole('button', { name: unused.title }));
  fireEvent.click(within(screen.getByRole('dialog')).getByRole('button', { name: '打开 阅读顺序' }));
  if (action === 'return') fireEvent.click(screen.getByRole('button', { name: '返回资料库' }));
  else if (action === 'select') fireEvent.click(screen.getByRole('button', { name: reask.title }));
  else fireEvent.click(screen.getByRole('button', { name: '关闭' }));
  await act(async () => { pending.resolve(reply({ note: { document_id: 'doc-one', title: '阅读顺序', markdown: '迟到的旧正文', revision: 3, verified: true, facts: [] }, grown: [] })); });
  expect(screen.queryByText('迟到的旧正文')).not.toBeInTheDocument();
  expect(screen.queryByRole('dialog', { name: '整理稿' })).not.toBeInTheDocument();
  if (action === 'select') expect(screen.getByRole('dialog', { name: '纠偏' })).toHaveTextContent(reask.evidence.questions[1]);
  else expect(screen.queryByRole('dialog')).not.toBeInTheDocument();
  expect(decisions).toHaveLength(0);
});
