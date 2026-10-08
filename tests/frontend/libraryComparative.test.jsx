import { act, cleanup, fireEvent, render, screen, within } from '@testing-library/react';
import { afterEach, beforeEach, expect, it, vi } from 'vitest';
import { Library } from '@src/features/library/Library';

const old = { id: 'old', project_id: 'alpha', text: '旧认识正文', revision: 1, conditions: ['阅读时'],
  kind: 'recognition', state: 'active', source_count: 1 };
const candidate = relation => ({ id: relation, text: `新认识 ${relation}`, kind: 'candidate',
  state: 'pending', revision: 1, source_count: 1, conditions: [], document_ids: [],
  hint: { relation, target_id: relation === 'new' ? null : old.id, target: relation === 'new' ? null : old,
    scope_hint: relation === 'new' ? 'me' : 'beta' } });
let rows, readonly, published, pendingRelation;
const settle = () => act(async () => { await new Promise(resolve => setTimeout(resolve, 0)); });
const mount = () => render(<Library projectId="alpha" projects={[
  { id: 'alpha', name: '阅读', scenes: [] }, { id: 'beta', name: '礼物', scenes: [] },
  { id: 'me', name: '我', scenes: [] }]} />);
beforeEach(() => {
  rows = ['duplicate_of', 'may_supersede', 'supplement', 'differs', 'new'].map(candidate);
  readonly = false;
  published = false; pendingRelation = true;
  vi.stubGlobal('fetch', vi.fn(async (url, options = {}) => {
    const path = new URL(String(url), 'http://localhost');
    let value = { items: [] };
    if (path.pathname.endsWith('/drill')) value = readonly ? {
      insight: { ...rows[0], kind: 'recognition', state: 'active' }, readonly: true, source_project_id: 'origin',
      note: { document_id: 'doc', title: '原处整理稿', revision: 1, verified: false, markdown: '正文', facts: [] },
      source: { id: 'source', title: '原件', kind: 'text', window: { pre: '', quote: '证据', post: '' } }, grown: []
    } : { insight: rows.find(row => row.id === path.searchParams.get('id')) || old, grown: [] };
    else if (path.pathname.endsWith('/insights')) value = { items: rows, counts: { pending: rows.length } };
    else if (path.pathname.endsWith('/file')) { rows = []; value = { id: 'published', state: 'active' }; }
    else if (path.pathname.endsWith('/confirm')) {
      published = true;
      rows = [{ id: 'published', project_id: 'alpha', text: '确认后的新认识',
        revision: 1, kind: 'recognition', state: 'active', conditions: [], source_count: 1 }, old];
      value = rows[0];
    }
    else if (path.pathname.endsWith('/links')) value = { links: published ? [{ id: 'proposal',
      kind: 'supersedes', other_id: old.id, state: pendingRelation ? 'suggested' : 'active' }] : [] };
    else if (path.pathname.endsWith('/relation-proposals')) value = { proposals: [{
      id: 'proposal', revision: 1, evidence: '新条件替代旧主张' }] };
    else if (path.pathname.endsWith('/link-suggestions/proposal/accept')) {
      pendingRelation = false; value = { id: 'proposal', state: 'accepted' };
    }
    else if (path.pathname.endsWith('/text')) value = { text: '原处全文' };
    else if (path.pathname.endsWith('/evidence-support')) value = { items: [{ id: 'support',
      state: 'pending', evidence: '新证据', revision: 1, documents: [] }] };
    return { ok: true, json: async () => value };
  }));
});
afterEach(() => { cleanup(); vi.unstubAllGlobals(); });

it('renders the five relation icons and the readonly old comparison', async () => {
  const { container } = mount(); await settle();
  for (const relation of rows.map(row => row.hint.relation)) {
    expect(container.querySelector(`[data-icon="${relation}"]`)).not.toBeNull();
  }
  fireEvent.click(screen.getByRole('button', { name: '新认识 may_supersede' })); await settle();
  const comparison = within(screen.getByRole('region', { name: '旧认识' }));
  expect(comparison.getByText('旧认识正文')).toBeVisible();
  expect(comparison.queryByRole('button', { name: '编辑' })).toBeNull();
});

it.each([['new', '确认到我', 'me'], ['supplement', '确认到 #礼物', 'beta']])(
  'confirms %s only after the explicit destination click', async (relation, label, destination) => {
    mount(); await settle();
    fireEvent.click(screen.getByRole('button', { name: `新认识 ${relation}` })); await settle();
    expect(fetch.mock.calls.filter(([, options]) => options?.method === 'POST').map(([url]) => url)
      .some(url => String(url).endsWith('/file'))).toBe(false);
    fireEvent.click(screen.getByRole('button', { name: label })); await settle();
    const call = fetch.mock.calls.find(([url]) => String(url).endsWith('/file'));
    expect(JSON.parse(call[1].body)).toEqual({ source_project_id: 'alpha', target_project_id: destination,
      expected_revision: 1, confirm: true });
    expect(screen.queryByRole('dialog')).toBeNull();
  });

it('reads the original owner and hides original write controls for a copied insight drill', async () => {
  readonly = true;
  mount(); await settle();
  fireEvent.click(screen.getByRole('button', { name: '新认识 duplicate_of' })); await settle();
  fireEvent.click(within(screen.getByRole('dialog')).getByRole('button', { name: '整理稿' })); await settle();
  expect(screen.queryByRole('button', { name: '核对完成' })).toBeNull();
  expect(within(screen.getByRole('dialog')).queryByRole('button', { name: '遗忘' })).toBeNull();
  fireEvent.click(within(screen.getByRole('dialog')).getByRole('button', { name: '原件' })); await settle();
  fireEvent.click(screen.getByRole('button', { name: '展开全文' })); await settle();
  const call = fetch.mock.calls.find(([url]) => String(url).includes('/sources/source/text'));
  expect(new URL(call[0], 'http://localhost').searchParams.get('project_id')).toBe('origin');
  expect(screen.queryByRole('button', { name: /私密/ })).toBeNull();
});

it('keeps evidence suggestions readable but removes their write actions in the old readonly view', async () => {
  mount(); await settle();
  fireEvent.click(screen.getByRole('button', { name: '新认识 may_supersede' })); await settle();
  fireEvent.click(screen.getByRole('button', { name: '打开旧认识' })); await settle();
  fireEvent.click(screen.getByRole('button', { name: '支持 1' })); await settle();
  expect(screen.getByText('新证据')).toBeVisible();
  expect(screen.queryByRole('button', { name: '确认支持' })).toBeNull();
  expect(screen.queryByRole('button', { name: '忽略支持' })).toBeNull();
  expect(screen.queryByRole('button', { name: '编辑' })).toBeNull();
});

it('keeps replacement confirmation and the existing merge reachable after publishing a comparison', async () => {
  mount(); await settle();
  fireEvent.click(screen.getByRole('button', { name: '新认识 may_supersede' })); await settle();
  fireEvent.click(within(screen.getByRole('dialog')).getByRole('button', { name: '确认', exact: true }));
  await settle();
  fireEvent.click(screen.getByRole('button', { name: '取代 1' })); await settle();
  expect(screen.getByText('新条件替代旧主张')).toBeVisible();
  expect(fetch.mock.calls.some(([url, options]) => String(url).endsWith('/link-suggestions/proposal/accept')
    && options?.method === 'POST')).toBe(false);
  fireEvent.click(screen.getByRole('button', { name: '确认连接' })); await settle();
  const review = fetch.mock.calls.find(([url, options]) => String(url).endsWith('/link-suggestions/proposal/accept')
    && options?.method === 'POST');
  expect(JSON.parse(review[1].body)).toEqual({ project_id: 'alpha', expected_revision: 1 });
  fireEvent.click(within(screen.getByRole('dialog')).getByRole('button', { name: '更多' }));
  fireEvent.click(screen.getByRole('button', { name: '合并', exact: true }));
  expect(screen.getByRole('textbox', { name: '合并正文' })).toBeVisible();
  expect(screen.getByRole('checkbox', { name: '旧认识正文' })).toBeVisible();
  expect(fetch.mock.calls.some(([url, options]) => String(url).endsWith('/merge')
    && options?.method === 'POST')).toBe(false);
});
