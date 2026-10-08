import { act, cleanup, fireEvent, render, screen, within } from '@testing-library/react';
import { afterEach, beforeEach, expect, it, vi } from 'vitest';
import { Library } from '@src/features/library/Library';

const insight = { id: 'recognition-one', text: '以证据支持判断', kind: 'recognition', state: 'active', revision: 1,
  scene: '阅读', source_count: 1, conditions: [], related: [], document_ids: ['doc-one'] };
const pending = { ...insight, id: 'candidate-one', text: '保留具体数字', kind: 'candidate', state: 'pending' };
const doc = { document_id: 'doc-one', title: '一次阅读', summary: '真实摘要', verified: false, revision: 1 };
const source = { id: 'source-one', title: '原件标题', kind: 'text', document_id: 'doc-one' };
const drill = { insight, grown: [insight], summary: { ...doc, text: '真实摘要' },
  note: { ...doc, markdown: '# 一次阅读\n正文\n## 待办\n- 核实数字', facts: [], todos: ['核实数字'] },
  source: { ...source, window: { pre: '原文前', quote: '真实证据', post: '原文后' } } };
let insights;
beforeEach(() => {
  insights = [insight, pending];
  vi.stubGlobal('fetch', vi.fn(async (url, options = {}) => {
    const path = new URL(String(url), 'http://localhost');
    let value;
    if (path.pathname.endsWith('/drill')) value = path.searchParams.get('id') === pending.id ? { ...drill, insight: pending } : drill;
    else if (options.method === 'POST' && path.pathname.endsWith('/verify')) value = { document_id: doc.document_id, verified: true };
    else if (options.method === 'POST' && path.pathname.endsWith('/forget')) {
      const forgotten = JSON.parse(options.body).forgotten;
      insights = insights.map(row => row.id === insight.id ? { ...row, state: forgotten ? 'forgotten' : 'active' } : row);
      value = insights[0];
    } else if (options.method === 'POST' && path.pathname.endsWith('/confirm')) {
      insights = insights.map(row => row.id === pending.id ? { ...row, id: 'published-two', state: 'active' } : row);
      value = insights[1];
    } else if (path.pathname.endsWith('/insights')) value = { items: insights, counts: {
      pending: insights.filter(r => r.state === 'pending').length, active: insights.filter(r => r.state === 'active').length,
      stale: 0, forgotten: insights.filter(r => r.state === 'forgotten').length } };
    else if (path.pathname.endsWith('/documents-archived')) value = { items: [] };
    else if (path.pathname.endsWith('/constraints')) value = { items: [{ enabled: true }, { enabled: false }] };
    else if (path.pathname.endsWith('/consolidate')) value = { score: 7, limit: false, running: false, job_id: null };
    else if (path.pathname.endsWith('/workbench')) value = { mental_models: [{ id: 'memo', question: '如何阅读？', answer: '核对依据', recognition_ids: [insight.id], evidence_count: 1, updated_at: '2026-10-01' }] };
    else value = { items: path.pathname.endsWith('/summaries') ? [doc] : path.pathname.endsWith('/notes') ? [doc] : [source] };
    return { ok: true, json: async () => value };
  }));
});
afterEach(() => { cleanup(); vi.unstubAllGlobals(); });
const mount = () => render(<Library projectId="alpha" projects={[{ id: 'alpha', name: '阅读项目', scenes: ['阅读'] }]}/>);
const settle = async () => act(async () => {});

it('selects only the opened insight when its multiple sources have no chosen note', async () => {
  const originalFetch = fetch;
  vi.stubGlobal('fetch', vi.fn(async (url, options = {}) => {
    const path = new URL(String(url), 'http://localhost');
    if (path.pathname.endsWith('/drill')) return { ok: true, json: async () => ({
      insight: pending, documents: [{ document_id: 'doc-one', title: '第一份依据' },
        { document_id: 'doc-two', title: '第二份依据' }], grown: [],
    }) };
    return originalFetch(url, options);
  }));
  mount(); await settle();
  fireEvent.click(screen.getByRole('button', { name: pending.text })); await settle();
  expect(screen.getByRole('button', { name: pending.text })).toHaveAttribute('aria-pressed', 'true');
  expect(screen.getByRole('button', { name: insight.text })).toHaveAttribute('aria-pressed', 'false');
  expect(screen.getByRole('region', { name: '整理稿选择' })).toBeInTheDocument();
});

it('opens the exact note passed by a tray deep link', async () => {
  render(<Library projectId="alpha" initialLayer="note" documentId="doc-one"/>); await settle();
  expect(screen.getByRole('dialog')).toHaveTextContent('一次阅读');
  expect(screen.getByRole('dialog')).toHaveTextContent('核实数字');
  const request = fetch.mock.calls.map(([url]) => new URL(String(url), 'http://localhost')).find(url => url.pathname.endsWith('/drill'));
  expect(request.searchParams.get('project_id')).toBe('alpha');
  expect(request.searchParams.get('from')).toBe('note');
  expect(request.searchParams.get('id')).toBe('doc-one');
});
it('opens the note list for historical tray jobs without a document', async () => {
  render(<Library projectId="alpha" initialLayer="note"/>); await settle();
  expect(screen.getByRole('button', {name:'整理稿 1'})).toHaveAttribute('aria-pressed','true');
  expect(screen.queryByRole('dialog')).not.toBeInTheDocument();
});

it('switches four layers with real counts and filters', async () => {
  mount(); await settle();
  expect(screen.getByRole('button', { name: '认识 2' })).toBeInTheDocument();
  expect(screen.getByRole('button', { name: '待确认 1' })).toBeInTheDocument();
  fireEvent.click(screen.getByRole('button', { name: '待确认 1' }));
  expect(screen.queryByRole('button', { name: '以证据支持判断' })).not.toBeInTheDocument();
  expect(screen.getByRole('button', { name: '保留具体数字' })).toBeInTheDocument();
  fireEvent.click(screen.getByRole('button', { name: '摘要 1' }));
  expect(screen.getByRole('button', { name: /一次阅读/ })).toBeInTheDocument();
  expect(screen.queryByRole('group', { name: '认识状态' })).not.toBeInTheDocument();
  fireEvent.click(screen.getByRole('button', { name: '原件 1' }));
  expect(screen.getByRole('button', { name: '原件标题' })).toBeInTheDocument();
});
it('opens breadcrumbs, moves down and reverses to grown insights', async () => {
  mount(); await settle(); fireEvent.click(screen.getByRole('button', { name: '以证据支持判断' })); await settle();
  const panel = screen.getByRole('dialog', { name: '认识' });
  fireEvent.click(within(panel).getByRole('button', { name: '下一层' }));
  expect(screen.getByRole('dialog', { name: '摘要' })).toHaveTextContent('真实摘要');
  fireEvent.click(within(screen.getByRole('dialog')).getByRole('button', { name: '原件' }));
  expect(screen.getByRole('dialog', { name: '原件' })).toHaveTextContent('真实证据');
  fireEvent.click(within(screen.getByRole('dialog')).getByRole('button', { name: '认识' }));
  expect(screen.getByRole('dialog')).toHaveTextContent('以证据支持判断');
  fireEvent.click(screen.getByRole('button', { name: '关闭' }));
  fireEvent.click(screen.getByRole('button', { name: '原件 1' }));
  fireEvent.click(screen.getByRole('button', { name: '原件标题' })); await settle();
  fireEvent.click(within(screen.getByRole('dialog')).getByRole('button', { name: '认识' }));
  expect(screen.getByRole('dialog')).toHaveTextContent('长出的认识');
});
it('forgets and restores using actual mutation bodies', async () => {
  mount(); await settle(); fireEvent.click(screen.getByRole('button', { name: '以证据支持判断' })); await settle();
  fireEvent.click(screen.getByRole('button', { name: '遗忘' })); await settle();
  expect(screen.getByRole('button', { name: '已遗忘 1' })).toBeInTheDocument();
  fireEvent.click(screen.getByRole('button', { name: '恢复' })); await settle();
  expect(screen.getByRole('button', { name: '已遗忘 0' })).toBeInTheDocument();
  const calls = fetch.mock.calls.filter(([url]) => String(url).endsWith('/forget'));
  expect(calls.map(([, options]) => JSON.parse(options.body))).toEqual([
    { project_id: 'alpha', forgotten: true }, { project_id: 'alpha', forgotten: false }]);
});
it('picks a faded candidate and returns it to pending without publishing', async () => {
  const faded = { ...pending, state: 'forgotten', recall_by: 'auto' };
  insights = [insight, faded];
  const originalFetch = fetch;
  vi.stubGlobal('fetch', vi.fn(async (url, options = {}) => {
    const path = new URL(String(url), 'http://localhost');
    if (options.method === 'POST' && path.pathname.endsWith(`/insights/${pending.id}/forget`)) {
      insights = [insight, pending];
      return { ok: true, json: async () => pending };
    }
    if (path.pathname.endsWith('/drill') && path.searchParams.get('id') === pending.id) {
      return { ok: true, json: async () => ({ ...drill, insight: insights[1] }) };
    }
    return originalFetch(url, options);
  }));
  mount(); await settle();
  fireEvent.click(screen.getByRole('button', { name: '已遗忘 1' }));
  fireEvent.click(screen.getByRole('button', { name: pending.text })); await settle();
  expect(screen.queryByRole('button', { name: '恢复' })).not.toBeInTheDocument();
  fireEvent.click(screen.getByRole('button', { name: '捡回' })); await settle();
  expect(screen.getByRole('button', { name: '待确认 1' })).toBeInTheDocument();
  expect(screen.getByRole('button', { name: '确认' })).toBeInTheDocument();
  const call = fetch.mock.calls.find(([url]) => String(url).endsWith(`/insights/${pending.id}/forget`));
  expect(JSON.parse(call[1].body)).toEqual({ project_id: 'alpha', forgotten: false });
});
it('loads scene and search and clears old selection on project change', async () => {
  const view = mount(); await settle();
  fireEvent.click(screen.getByRole('button', { name: '阅读' })); await settle();
  expect(fetch.mock.calls.some(([url]) => String(url).includes('scene='))).toBe(true);
  fireEvent.change(screen.getByRole('searchbox', { name: '搜索' }), { target: { value: '证据' } }); await settle();
  expect(fetch.mock.calls.some(([url]) => new URL(String(url), 'http://localhost').searchParams.get('q') === '证据')).toBe(true);
  fireEvent.click(screen.getByRole('button', { name: '以证据支持判断' })); await settle();
  view.rerender(<Library projectId="beta" projects={[]}/>);
  expect(screen.queryByRole('dialog')).not.toBeInTheDocument();
});
it('renders organized Markdown as reading content and submits verification', async () => {
  mount(); await settle(); fireEvent.click(screen.getByRole('button', { name: '整理稿 1' }));
  fireEvent.click(screen.getByRole('button', { name: /一次阅读/ })); await settle();
  const panel = screen.getByRole('dialog', { name: '整理稿' });
  expect(within(panel).getByRole('heading', { name: '一次阅读', level: 1 })).toBeInTheDocument();
  fireEvent.click(within(panel).getByRole('button', { name: '核对完成' })); await settle();
  const call = fetch.mock.calls.find(([url]) => String(url).endsWith('/verify'));
  expect(JSON.parse(call[1].body)).toEqual({ project_id: 'alpha', document_revision: 1 });
  expect(within(panel).getByText('已核对')).toBeInTheDocument();
});
it('confirms a pending insight and refreshes list counts', async () => {
  mount(); await settle(); fireEvent.click(screen.getByRole('button', { name: '保留具体数字' })); await settle();
  fireEvent.click(screen.getByRole('button', { name: '确认' })); await settle();
  expect(screen.getByRole('button', { name: '待确认 0' })).toBeInTheDocument();
  expect(screen.queryByRole('button', { name: '确认' })).not.toBeInTheDocument();
  const call = fetch.mock.calls.find(([url]) => String(url).endsWith('/confirm'));
  expect(JSON.parse(call[1].body)).toEqual({ project_id: 'alpha', expected_revision: 1 });
});
it('keeps only scope in the rail and opens the three-item library menu', async () => {
  const navigate = vi.fn();
  render(<Library projectId="alpha" projects={[]} onNavigate={navigate}/>); await settle();
  const rail = screen.getByRole('complementary', { name: '场景' });
  expect(within(rail).queryByRole('button', { name: '约束' })).not.toBeInTheDocument();
  expect(within(rail).queryByText('固定问题')).not.toBeInTheDocument();
  expect(within(rail).queryByText('已归档')).not.toBeInTheDocument();
  fireEvent.click(screen.getByRole('button', { name: '资料库更多' }));
  const menu = screen.getByRole('menu', { name: '资料库更多' });
  expect(within(menu).getAllByRole('menuitem').map(item => item.textContent.trim())).toEqual(['现在整理 7/10', '约束 1', '备忘']);
  fireEvent.click(within(menu).getByRole('menuitem', { name: '约束 1' }));
  expect(navigate).toHaveBeenCalledWith('settings', { project_id: 'alpha', section: 'project', expand_project: 'alpha' });
  fireEvent.click(screen.getByRole('button', { name: '资料库更多' }));
  fireEvent.click(screen.getByRole('menuitem', { name: '备忘' })); await settle();
  const panel = screen.getByRole('dialog', { name: '备忘' });
  expect(panel).toHaveTextContent('如何阅读？'); expect(panel).toHaveTextContent('核对依据');
  expect(panel).toHaveTextContent('2026·10·01'); expect(panel).toHaveTextContent('认 1');
  expect(within(panel).queryByRole('button', { name: /新增|刷新|生成/ })).not.toBeInTheDocument();
});
it('opens published insight versions from more without exposing it to candidates', async () => {
  mount(); await settle(); fireEvent.click(screen.getByRole('button', { name: '以证据支持判断' })); await settle();
  fireEvent.click(screen.getByRole('button', { name: '更多', exact: true }));
  fireEvent.click(screen.getByRole('button', { name: '版本', exact: true })); await settle();
  expect(screen.getByText(/修改历史/)).toBeInTheDocument();
  fireEvent.click(screen.getByRole('button', { name: '关闭' }));
  fireEvent.click(screen.getByRole('button', { name: '保留具体数字' })); await settle();
  expect(screen.queryByRole('button', { name: '更多', exact: true })).not.toBeInTheDocument();
});

it('counts three note states and forgets/restores without hiding the original', async () => {
  let archived = false;
  const base = fetch.getMockImplementation();
  fetch.mockImplementation(async (url, options = {}) => {
    const path = new URL(String(url), 'http://localhost').pathname;
    let value;
    if (path.endsWith('/archive') || path.endsWith('/restore')) {
      archived = path.endsWith('/archive'); value = { document_id: doc.document_id, revision: archived ? 2 : 3 };
    } else if (path.endsWith('/documents-archived')) value = { items: archived ? [{ ...doc, revision: 2 }] : [] };
    else if (path.endsWith('/notes')) value = { items: [{ ...doc, revision: archived ? 2 : 3 }, { ...doc, document_id: 'checked', title: '核对稿', verified: true }] };
    else if (path.endsWith('/summaries')) value = { items: archived ? [] : [doc] };
    else if (path.endsWith('/drill')) value = { ...drill, note: { ...drill.note, revision: archived ? 2 : 3 } };
    else return base(url, options);
    return { ok: true, json: async () => value };
  });
  mount(); await settle(); fireEvent.click(screen.getByRole('button', { name: '整理稿 2' }));
  expect(screen.getByRole('button', { name: '未核对 1' })).toBeInTheDocument();
  expect(screen.getByRole('button', { name: '已核对 1' })).toBeInTheDocument();
  expect(screen.getByRole('button', { name: '已遗忘 0' })).toBeInTheDocument();
  fireEvent.click(screen.getByRole('button', { name: '未核对 1' }));
  expect(screen.queryByRole('button', { name: '核对稿' })).not.toBeInTheDocument();
  fireEvent.click(screen.getByRole('button', { name: '一次阅读' })); await settle();
  fireEvent.click(screen.getByRole('button', { name: '遗忘' })); await settle();
  expect(screen.getByRole('button', { name: '未核对 0' })).toBeInTheDocument();
  expect(screen.getByRole('button', { name: '已遗忘 1' })).toBeInTheDocument();
  expect(screen.getByRole('button', { name: '摘要 0' })).toBeInTheDocument();
  expect(screen.getByRole('button', { name: '原件 1' })).toBeInTheDocument();
  fireEvent.click(screen.getByRole('button', { name: '已遗忘 1' }));
  fireEvent.click(screen.getByRole('button', { name: '一次阅读' })); await settle();
  expect(screen.queryByRole('button', { name: '核对完成' })).not.toBeInTheDocument();
  fireEvent.click(screen.getByRole('button', { name: '恢复' })); await settle();
  expect(screen.getByRole('button', { name: '已遗忘 0' })).toBeInTheDocument();
  expect(screen.getByRole('button', { name: '摘要 1' })).toBeInTheDocument();
  const calls = fetch.mock.calls.filter(([url]) => /\/(archive|restore)\?/.test(String(url)));
  expect(calls.map(([, options]) => JSON.parse(options.body))).toEqual([{ expected_revision: 3 }, { expected_revision: 2 }]);
});

it('keeps layer counts unknown while their read is pending', () => {
 fetch.mockImplementation(() => new Promise(() => {})); render(<Library projectId="alpha" projects={[]}/>);
 for (const label of ['认识', '摘要', '整理稿', '原件']) expect(screen.getByRole('button', { name: `${label} —` })).toBeInTheDocument();
});
