import { act, cleanup, fireEvent, render, screen } from '@testing-library/react';
import { afterEach, expect, it, vi } from 'vitest';
import { Library } from '@src/features/library/Library';

afterEach(() => { cleanup(); vi.unstubAllGlobals(); });
const response = (value, status = 200) => ({ ok: status < 400, status, json: async () => value, text: async () => JSON.stringify(value) });
const note = { document_id: 'synthetic-note', title: '合成整理稿', revision: 4, markdown: '# 合成整理稿\r\n\r\n正文。\r\n', facts: [], verified: false };
function setup({ readonly = false, conflict = false } = {}) {
  let current = { ...note };
  vi.stubGlobal('fetch', vi.fn(async (url, options = {}) => {
    const path = new URL(String(url), 'http://localhost').pathname;
    if (path.endsWith('/drill')) return response({ note: current, readonly });
    if (path.endsWith('/documents/synthetic-note') && options.method === 'PATCH') {
      if (conflict) { conflict = false; current = { ...note, revision: 7, markdown: '服务器稿。' }; return response({ detail: 'conflict' }, 409); }
      current = { ...current, markdown: JSON.parse(options.body).markdown, revision: current.revision + 1 }; return response(current);
    }
    if (path.endsWith('/documents/synthetic-note')) return response(current);
    if (path.endsWith('/verify')) return response({ document_id: note.document_id, verified: true });
    if (path.endsWith('/consolidate')) return response({ score: 7 });
    return response({ items: path.endsWith('/notes') ? [current] : [], counts: {} });
  }));
}
async function mount() { render(<Library projectId="alpha" initialLayer="note" documentId={note.document_id}/>); await act(async () => {}); }
function edit() {
  fireEvent.click(screen.getByRole('button', { name: '更多编辑' })); fireEvent.click(screen.getByRole('button', { name: '源码' }));
  fireEvent.change(screen.getByRole('textbox', { name: '整理稿正文' }), { target: { value: '# 修改稿\n\n正文。\n' } });
}
it('edits the real library note entry and carries its saved revision to lifecycle actions', async () => {
  setup(); await mount(); expect(screen.queryByRole('textbox', { name: '整理稿正文' })).not.toBeInTheDocument(); edit();
  fireEvent.click(screen.getByRole('button', { name: '保存' })); await act(async () => {});
  const save = fetch.mock.calls.find(([, options]) => options.method === 'PATCH');
  expect(save[0]).toBe('/api/recognition/documents/synthetic-note');
  expect(JSON.parse(save[1].body)).toEqual({ project_id: 'alpha', expected_revision: 4, markdown: '# 修改稿\n\n正文。\n' });
  expect(screen.queryByRole('button', { name: '核对完成' })).not.toBeInTheDocument();
  fireEvent.click(screen.getByRole('button', { name: '遗忘' })); await act(async () => {});
  const archive = fetch.mock.calls.find(([url]) => new URL(String(url), 'http://localhost').pathname.endsWith('/archive'));
  expect(JSON.parse(archive[1].body)).toEqual({ expected_revision: 5 });
});
it('keeps base, local, and server at a real library save conflict and retries current CAS', async () => {
  setup({ conflict: true }); await mount(); edit(); fireEvent.click(screen.getByRole('button', { name: '保存' })); await act(async () => {});
  const alert = screen.getByRole('alert', { name: '草稿版本冲突' });
  expect(alert.textContent).toContain(note.markdown); expect(alert).toHaveTextContent('修改稿'); expect(alert).toHaveTextContent('服务器稿。');
  fireEvent.click(screen.getByRole('button', { name: '保留本地内容继续编辑' })); fireEvent.click(screen.getByRole('button', { name: '保存' })); await act(async () => {});
  const saves = fetch.mock.calls.filter(([, options]) => options.method === 'PATCH');
  expect(JSON.parse(saves[1][1].body)).toEqual({ project_id: 'alpha', expected_revision: 7, markdown: '# 修改稿\n\n正文。\n' });
});
it('keeps a readonly drill note readable without edit or mutation controls', async () => {
  setup({ readonly: true }); await mount(); expect(screen.getByRole('heading', { name: '合成整理稿' })).toBeInTheDocument();
  expect(screen.queryByRole('button', { name: '更多编辑' })).not.toBeInTheDocument(); expect(screen.queryByRole('button', { name: '保存' })).not.toBeInTheDocument();
  expect(screen.queryByRole('button', { name: '核对完成' })).not.toBeInTheDocument();
  expect(fetch.mock.calls.filter(([, options]) => options.method === 'PATCH')).toHaveLength(0);
});
