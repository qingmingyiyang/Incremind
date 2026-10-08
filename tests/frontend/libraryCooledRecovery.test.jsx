import { act, cleanup, fireEvent, render, screen } from '@testing-library/react';
import { afterEach, expect, it, vi } from 'vitest';
import { Library } from '@src/features/library/Library';

afterEach(() => { cleanup(); vi.unstubAllGlobals(); });
const settle = async () => act(async () => {});

function transport({ kind = 'insight', verified = false, archived = false, readonly = false } = {}) {
  let cooled = true;
  const insight = () => ({ id: 'recognition-cooled', text: '冷却认识', kind: 'recognition',
    state: 'active', recall_state: cooled ? 'cooled' : 'normal', revision: 1,
    source_count: 1, document_ids: [], conditions: [], related: [] });
  const note = () => ({ document_id: 'doc-cooled', title: '冷却整理稿', revision: 2,
    verified, recall_state: cooled ? 'cooled' : 'normal', recall_preference_revision: cooled ? 3 : 4,
    markdown: '# 冷却整理稿\n\n保留原正文', facts: [], todos: [] });
  const fetcher = vi.fn(async (url, options = {}) => {
    const path = new URL(String(url), 'http://localhost');
    let value = { items: [] };
    if (options.method === 'POST' && path.pathname.endsWith('/forget')) {
      expect(JSON.parse(options.body)).toEqual({ project_id: 'alpha', forgotten: false });
      cooled = false; value = insight();
    } else if (options.method === 'POST' && path.pathname.endsWith('/restore-recall')) {
      expect(JSON.parse(options.body)).toEqual({ project_id: 'alpha', document_revision: 2, preference_revision: 3 });
      cooled = false; value = { document_id: 'doc-cooled', recall_state: 'normal', recall_preference_revision: 4 };
    } else if (options.method === 'POST' && path.pathname.endsWith('/restore')) {
      value = note();
    } else if (path.pathname.endsWith('/drill')) {
      value = kind === 'insight' ? { insight: insight(), grown: [] }
        : { note: note(), summary: { document_id: 'doc-cooled', title: '冷却整理稿', text: '保留摘要' }, readonly };
    } else if (path.pathname.endsWith('/insights')) {
      value = { items: kind === 'insight' ? [insight()] : [], counts: { active: kind === 'insight' ? 1 : 0 } };
    } else if (path.pathname.endsWith('/notes')) value = { items: kind === 'note' ? [note()] : [] };
    else if (path.pathname.endsWith('/documents-archived')) value = { items: archived ? [note()] : [] };
    else if (path.pathname.endsWith('/consolidate')) value = { score: 0, running: false, limit: false };
    return { ok: true, json: async () => value };
  });
  vi.stubGlobal('fetch', fetcher);
  return fetcher;
}

function mount(kind = 'insight') {
  render(<Library projectId="alpha" initialLayer={kind} projects={[{ id: 'alpha', name: '项目' }]}/>);
}

it('restores an active cooled recognition through the existing forget endpoint', async () => {
  const fetcher = transport(); mount(); await settle();
  fireEvent.click(screen.getByRole('button', { name: '冷却认识' })); await settle();
  expect(screen.queryByRole('button', { name: '遗忘' })).not.toBeInTheDocument();
  fireEvent.click(screen.getByRole('button', { name: '恢复' })); await settle();
  expect(screen.getByRole('button', { name: '遗忘' })).toBeInTheDocument();
  expect(fetcher.mock.calls.filter(([, options]) => options.method === 'POST' && options.body?.includes('forgotten'))).toHaveLength(1);
});

it.each([false, true])('restores a cooled note while preserving verified=%s', async verified => {
  const fetcher = transport({ kind: 'note', verified }); mount('note'); await settle();
  fireEvent.click(screen.getByRole('button', { name: '冷却整理稿' })); await settle();
  expect(screen.queryByRole('button', { name: '遗忘' })).not.toBeInTheDocument();
  fireEvent.click(screen.getByRole('button', { name: '恢复' })); await settle();
  fireEvent.click(screen.getByRole('button', { name: '冷却整理稿' })); await settle();
  expect(screen.getByRole('button', { name: '遗忘' })).toBeInTheDocument();
  expect(Boolean(screen.queryByRole('button', { name: '核对完成' }))).toBe(!verified);
  const mutations = fetcher.mock.calls.filter(([url, options]) => options.method === 'POST' && /\/(restore-recall|archive|restore)(\?|$)/.test(String(url)));
  expect(mutations).toHaveLength(1);
  expect(String(mutations[0][0])).toContain('/api/v2/library/notes/doc-cooled/restore-recall');
});

it('preserves the original archived document restoration priority', async () => {
  const fetcher = transport({ kind: 'note', archived: true }); mount('note'); await settle();
  fireEvent.click(screen.getByRole('button', { name: '冷却整理稿' })); await settle();
  fireEvent.click(screen.getByRole('button', { name: '恢复' })); await settle();
  const calls = fetcher.mock.calls.filter(([url, options]) => options.method === 'POST' && /\/(restore-recall|restore)(\?|$)/.test(String(url)));
  expect(calls).toHaveLength(1);
  expect(String(calls[0][0])).toContain('/api/rebuild/documents/doc-cooled/restore?project_id=alpha');
  expect(JSON.parse(calls[0][1].body)).toEqual({ expected_revision: 2 });
});

it('keeps a read-only cooled note free of mutation actions', async () => {
  transport({ kind: 'note', readonly: true }); mount('note'); await settle();
  fireEvent.click(screen.getByRole('button', { name: '冷却整理稿' })); await settle();
  expect(screen.queryByRole('button', { name: '恢复' })).not.toBeInTheDocument();
  expect(screen.queryByRole('button', { name: '遗忘' })).not.toBeInTheDocument();
});
