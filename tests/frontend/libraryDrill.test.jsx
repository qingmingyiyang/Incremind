import { act, cleanup, fireEvent, render, screen, within } from '@testing-library/react';
import { afterEach, beforeEach, expect, it, vi } from 'vitest';
import { Library } from '@src/features/library/Library';

const insight = { id: 'i', text: '共同认识', state: 'active', source_count: 2, conditions: [] };
const documents = [{ document_id: 'd1', title: '第一稿' }, { document_id: 'd2', title: '第二稿' }];
const sources = [{ id: 's1', title: '第一原件', kind: 'text' }, { id: 's2', title: '第二原件', kind: 'text' }];
const note = id => ({ document_id: id, title: id === 'd1' ? '第一稿' : '第二稿', markdown: `# ${id}正文`, facts: [], revision: 1 });
let windowed, memos;
const settle = async () => act(async () => {});
const mount = () => render(<Library projectId="alpha" projects={[{ id: 'alpha', scenes: ['阅读'] }]}/>);
const open = async () => { mount(); await settle(); fireEvent.click(screen.getByRole('button', { name: '共同认识' })); await settle(); };
const panel = () => screen.getByRole('dialog');
const jump = name => fireEvent.click(within(panel()).getByRole('button', { name, exact: true }));
it('keeps the source project when opening and verifying evidence for a global insight', async () => {
  vi.stubGlobal('fetch', vi.fn(async (url, options = {}) => {
    const path = new URL(String(url), 'http://localhost');
    let value = { items: [] };
    if (path.pathname.endsWith('/insights')) value = { items: [insight], counts: { active: 1 } };
    else if (path.pathname.endsWith('/evidence-support')) value = { items: [{
      id: 'support', revision: 1, state: 'pending', current: true, project_id: 'alpha',
      evidence: '共同依据', conditions: [], documents: [{ id: 'd1', title: '第一稿' }],
    }] };
    else if (path.pathname.endsWith('/drill')) value = path.searchParams.get('from') === 'note'
      ? { insight: null, grown: [], documents: [documents[0]], sources: [],
          note: note('d1'), summary: { ...note('d1'), text: '第一稿摘要' } }
      : { insight, grown: [], documents: [], sources: [], note: null, summary: null };
    else if (path.pathname.endsWith('/verify')) value = { document_id: 'd1' };
    return { ok: true, json: async () => value };
  }));
  render(<Library projectId="me" projects={[{ id: 'me', scenes: [] }]}/>);
  await settle();
  fireEvent.click(screen.getByRole('button', { name: '共同认识' }));
  await settle();
  fireEvent.click(within(panel()).getByRole('button', { name: '支持 1' }));
  fireEvent.click(within(panel()).getByRole('button', { name: '打开整理稿 d1' }));
  await settle();
  fireEvent.click(within(panel()).getByRole('button', { name: '核对完成' }));
  await settle();
  jump('摘要');
  await settle();
  const requests = fetch.mock.calls.map(([url, options = {}]) => ({
    path: new URL(String(url), 'http://localhost'), body: options.body && JSON.parse(options.body),
  }));
  expect(requests.find(r => r.path.pathname.endsWith('/verify')).body.project_id).toBe('alpha');
  expect(requests.find(r => r.path.searchParams.get('from') === 'note').path.searchParams.get('project_id')).toBe('alpha');
  expect(requests.filter(r => r.path.pathname.endsWith('/usage/open')).slice(-2).map(r => r.body.project_id)).toEqual(['alpha', 'alpha']);
});
beforeEach(() => {
  windowed = false;
  memos = [{ id: 'memo', question: '备忘问题', answer: '备忘答案', updated_at: '2026-10-01T12:00:00', evidence_count: 3 }];
  vi.stubGlobal('fetch', vi.fn(async url => {
    const path = new URL(String(url), 'http://localhost');
    let value;
    if (path.pathname.endsWith('/drill')) {
      const documentId = path.searchParams.get('document_id');
      const sourceId = path.searchParams.get('source_id');
      const direct = path.searchParams.get('from') === 'source';
      value = { insight, grown: [], documents, sources: documentId || direct ? sources : [],
        summary: documentId ? { ...note(documentId), text: `${documentId}摘要` } : null,
        note: documentId ? note(documentId) : null,
        source: sourceId || direct ? { ...sources.find(s => s.id === (sourceId || path.searchParams.get('id'))), window: windowed ? { pre: '前', quote: '依据', post: '后' } : null } : null };
    } else if (/\/sources\/[^/]+\/text$/.test(path.pathname)) {
      const id = path.pathname.split('/').at(-2);
      value = { id, coordinate_space: 'source_content_v1', text: `${id}完整原文\n最后一行` };
    } else if (path.pathname.endsWith('/insights')) {
      value = path.searchParams.get('project_id') === 'inbox' ? { items: [], counts: { pending: 2, active: 5, stale: 11, forgotten: 13 } } : { items: [insight], counts: { active: 1 } };
    } else if (path.pathname.endsWith('/sources')) value = { items: sources };
    else if (path.pathname.endsWith('/workbench')) value = { mental_models: memos };
    else value = { items: [] };
    return { ok: true, json: async () => value };
  }));
});
afterEach(() => { cleanup(); vi.unstubAllGlobals(); });

it('requests a selected document and displays its summary', async () => {
  await open();
  fireEvent.click(within(panel()).getByRole('button', { name: '第二稿' })); await settle();
  jump('摘要');
  expect(panel()).toHaveTextContent('d2摘要');
  const request = fetch.mock.calls.map(([url]) => new URL(String(url), 'http://localhost')).find(url => url.searchParams.get('document_id') === 'd2');
  expect(request.searchParams.get('from')).toBe('insight'); expect(request.searchParams.get('id')).toBe('i');
});
const deferDrill = matches => {
  const base = fetch.getMockImplementation(); let finish;
  fetch.mockImplementation((url, options) => matches(new URL(String(url), 'http://localhost'))
    ? new Promise(resolve => { finish = async () => resolve(await base(url, options)); })
    : base(url, options));
  return () => act(async () => { await finish(); });
};
it('applies a document choice after navigating to its summary while the read is pending', async () => {
  const finish = deferDrill(url => url.pathname.endsWith('/drill') && url.searchParams.get('document_id') === 'd2');
  await open();
  fireEvent.click(within(panel()).getByRole('button', { name: '第二稿' }));
  jump('摘要');
  await finish();
  expect(panel()).toHaveAccessibleName('摘要');
  expect(panel()).toHaveTextContent('d2摘要');
  expect(within(panel()).getByRole('button', { name: '第二稿' })).toHaveAttribute('aria-pressed', 'true');
});
it('keeps a pending original choice when navigating within the same drill', async () => {
  const finish = deferDrill(url => url.pathname.endsWith('/drill') && url.searchParams.get('source_id') === 's2');
  await open(); fireEvent.click(within(panel()).getByRole('button', { name: '第一稿' })); await settle();
  jump('原件'); fireEvent.click(within(panel()).getByRole('button', { name: '第二原件' }));
  jump('整理稿'); await finish();
  expect(panel()).toHaveAccessibleName('整理稿');
  jump('原件'); await settle();
  expect(panel()).toHaveTextContent('s2完整原文');
  expect(within(panel()).getByRole('button', { name: '第二原件' })).toHaveAttribute('aria-pressed', 'true');
});
it('rejects an older document choice after a newer choice has loaded', async () => {
  const finish = deferDrill(url => url.pathname.endsWith('/drill') && url.searchParams.get('document_id') === 'd1');
  await open(); fireEvent.click(within(panel()).getByRole('button', { name: '第一稿' }));
  fireEvent.click(within(panel()).getByRole('button', { name: '第二稿' })); await settle();
  jump('摘要'); await finish();
  expect(panel()).toHaveTextContent('d2摘要');
  expect(panel()).not.toHaveTextContent('d1摘要');
});
it('does not reopen a closed drill when a document choice resolves', async () => {
  const finish = deferDrill(url => url.pathname.endsWith('/drill') && url.searchParams.get('document_id') === 'd1');
  await open(); fireEvent.click(within(panel()).getByRole('button', { name: '第一稿' }));
  fireEvent.click(within(panel()).getByRole('button', { name: '关闭' })); await finish();
  expect(screen.queryByRole('dialog')).not.toBeInTheDocument();
});
it('discards a pending document choice after changing projects', async () => {
  const finish = deferDrill(url => url.pathname.endsWith('/drill') && url.searchParams.get('document_id') === 'd1');
  const view = mount(); await settle(); fireEvent.click(screen.getByRole('button', { name: '共同认识' })); await settle();
  fireEvent.click(within(panel()).getByRole('button', { name: '第一稿' }));
  view.rerender(<Library projectId="beta" projects={[]}/>); await settle(); await finish();
  expect(screen.queryByRole('dialog')).not.toBeInTheDocument();
});
it('selects an original and reads full text when it has no evidence window', async () => {
  await open(); fireEvent.click(within(panel()).getByRole('button', { name: '第一稿' })); await settle(); jump('原件');
  fireEvent.click(within(panel()).getByRole('button', { name: '第二原件' })); await settle();
  expect(panel()).toHaveTextContent('s2完整原文'); expect(panel()).toHaveTextContent('最后一行');
  expect(fetch.mock.calls.some(([url]) => String(url).includes('document_id=d1') && String(url).includes('source_id=s2'))).toBe(true);
  expect(fetch.mock.calls.some(([url]) => String(url).includes('/sources/s2/text?project_id=alpha'))).toBe(true);
});
it('keeps a highlighted window until full text is expanded', async () => {
  windowed = true;
  await open(); fireEvent.click(within(panel()).getByRole('button', { name: '第一稿' })); await settle(); jump('原件');
  fireEvent.click(within(panel()).getByRole('button', { name: '第一原件' })); await settle();
  expect(within(panel()).getByText('依据').tagName).toBe('MARK');
  expect(fetch.mock.calls.some(([url]) => String(url).includes('/sources/s1/text'))).toBe(false);
  fireEvent.click(within(panel()).getByRole('button', { name: '展开全文' })); await settle();
  expect(panel()).toHaveTextContent('s1完整原文');
});
it('reads a directly opened original even with multiple document candidates', async () => {
  mount(); await settle(); fireEvent.click(screen.getByRole('button', { name: '原件 2' }));
  fireEvent.click(screen.getByRole('button', { name: '第一原件' })); await settle();
  expect(panel()).toHaveTextContent('s1完整原文');
});
it('counts only inbox pending plus active independently of scene and search', async () => {
  mount(); await settle(); expect(screen.getByRole('button', { name: '收件箱 7' })).toBeInTheDocument();
  fireEvent.click(screen.getByRole('button', { name: '阅读' }));
  fireEvent.change(screen.getByRole('searchbox'), { target: { value: '限定' } }); await settle();
  expect(screen.getByRole('button', { name: '收件箱 7' })).toBeInTheDocument();
  const requests = fetch.mock.calls.map(([url]) => new URL(String(url), 'http://localhost')).filter(url => url.searchParams.get('project_id') === 'inbox');
  expect(requests.every(url => !url.searchParams.has('scene') && url.searchParams.get('q') === '')).toBe(true);
});
it('uses official memo date and evidence_count instead of legacy recognition ids', async () => {
  mount(); await settle(); fireEvent.click(screen.getByRole('button', { name: '资料库更多' })); fireEvent.click(screen.getByRole('menuitem', { name: '备忘' })); await settle();
  expect(panel()).toHaveTextContent('2026·10·01 · 认 3');
});
it('keeps zero memo evidence and marks unknown metadata with a dash', async () => {
  memos = [{ id: 'zero', question: '零依据', answer: '', updated_at: '', evidence_count: 0 }, { id: 'unknown', question: '未知依据', answer: '', updated_at: null, evidence_count: null }];
  mount(); await settle(); fireEvent.click(screen.getByRole('button', { name: '资料库更多' })); fireEvent.click(screen.getByRole('menuitem', { name: '备忘' })); await settle();
  expect(panel()).toHaveTextContent('— · 认 0'); expect(panel()).toHaveTextContent('— · 认 —');
});
it('ignores a full-text response after changing the project', async () => {
  const base = fetch.getMockImplementation(); let resolve;
  fetch.mockImplementation((url, options) => String(url).includes('/sources/s1/text') ? new Promise(done => { resolve = done; }) : base(url, options));
  const view = mount(); await settle(); fireEvent.click(screen.getByRole('button', { name: '原件 2' })); fireEvent.click(screen.getByRole('button', { name: '第一原件' })); await settle();
  view.rerender(<Library projectId="beta" projects={[]}/>); await settle();
  await act(async () => resolve({ ok: true, json: async () => ({ text: '旧项目正文' }) }));
  expect(screen.queryByText('旧项目正文')).not.toBeInTheDocument(); expect(screen.queryByRole('dialog')).not.toBeInTheDocument();
});
it('retries a failed full-text read without replacing the selected original', async () => {
  const base = fetch.getMockImplementation(); let attempts = 0;
  fetch.mockImplementation((url, options) => {
    if (String(url).includes('/sources/s1/text') && ++attempts === 1) return Promise.resolve({ ok: false, status: 503, json: async () => ({}) });
    return base(url, options);
  });
  mount(); await settle(); fireEvent.click(screen.getByRole('button', { name: '原件 2' })); fireEvent.click(screen.getByRole('button', { name: '第一原件' })); await settle();
  expect(within(panel()).getByRole('alert')).toHaveTextContent('读取未完成 · 重试');
  fireEvent.click(within(panel()).getByRole('button', { name: '重试' })); await settle();
  expect(panel()).toHaveTextContent('s1完整原文'); expect(attempts).toBe(2);
});
