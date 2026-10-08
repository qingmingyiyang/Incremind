import { act, cleanup, fireEvent, render, screen } from '@testing-library/react';
import { afterEach, expect, it, vi } from 'vitest';
import { DocumentMarkdownEditor } from '@src/shared/ui/DocumentMarkdownEditor';
import { Workbench } from '@src/features/workbench/Workbench';

afterEach(() => { cleanup(); vi.unstubAllGlobals(); });
const response = (value, status = 200) => ({ ok: status < 400, status, json: async () => value, text: async () => JSON.stringify(value) });
const document = { id: 'synthetic-doc', revision: 4, markdown: '# 合成成果\n\n原正文。' };
function source() { fireEvent.click(screen.getByRole('button', { name: '更多编辑' })); fireEvent.click(screen.getByRole('button', { name: '源码' })); }
it('saves the opened revision and local Markdown through the original document API', async () => {
  vi.stubGlobal('fetch', vi.fn(async () => response({ ...document, revision: 5 })));
  render(<DocumentMarkdownEditor projectId="alpha" documentId="synthetic-doc" document={document}/>);
  source(); fireEvent.change(screen.getByRole('textbox', { name: '整理稿正文' }), { target: { value: '本机成果。' } });
  fireEvent.click(screen.getByRole('button', { name: '保存' })); await act(async () => {});
  expect(JSON.parse(fetch.mock.calls[0][1].body)).toEqual({ project_id: 'alpha', expected_revision: 4, markdown: '本机成果。' });
});
it.each([false, true])('preserves all three snapshots at 409 and chooses server=%s', async useServer => {
  let conflict = true; const server = { ...document, revision: 7, markdown: '服务器成果。' };
  vi.stubGlobal('fetch', vi.fn(async (_url, options) => options.method === 'PATCH' ? response(conflict ? { detail: 'revision_conflict' } : { ...server, revision: 8 }, conflict ? 409 : 200) : response(server)));
  render(<DocumentMarkdownEditor projectId="alpha" documentId="synthetic-doc" document={document}/>);
  source(); fireEvent.change(screen.getByRole('textbox', { name: '整理稿正文' }), { target: { value: '本机成果。' } });
  fireEvent.click(screen.getByRole('button', { name: '保存' })); await act(async () => {});
  const alert = screen.getByRole('alert', { name: '草稿版本冲突' }); expect(alert.textContent).toContain(document.markdown); expect(alert).toHaveTextContent('服务器成果。'); expect(alert).toHaveTextContent('本机成果。');
  expect(screen.getByRole('button', { name: '保存' })).toBeDisabled();
  fireEvent.click(screen.getByRole('button', { name: useServer ? '采用服务器版本' : '保留本地内容继续编辑' }));
  conflict = false; fireEvent.click(screen.getByRole('button', { name: '保存' })); await act(async () => {});
  const saves = fetch.mock.calls.filter(([, options]) => options.method === 'PATCH');
  expect(JSON.parse(saves[1][1].body)).toEqual({ project_id: 'alpha', expected_revision: 7, markdown: useServer ? server.markdown : '本机成果。' });
});
it('does not enable a write when a read response lacks a revision', () => {
  render(<DocumentMarkdownEditor projectId="alpha" documentId="synthetic-doc" document={{ markdown: '只读内容。' }}/>);
  expect(screen.getByText('只读内容。')).toBeInTheDocument(); expect(screen.getByRole('button', { name: '保存' })).toBeDisabled();
});
it('uses the actual workbench outcome entry and its returned revision', async () => {
  vi.stubGlobal('fetch', vi.fn(async (url, options = {}) => {
    const path = String(url);
    if (path.includes('/documents/')) return response({ ...document, revision: options.method === 'PATCH' ? 5 : 4 });
    if (path.includes('/threads/thread-1')) return response({ id: 'thread-1', turns: [{ id: 'synthetic-turn', intent: 'do', receipt: { do: { title: '合成任务', state: 'done', document_id: document.id, progress: { done: 3, total: 3 } } } }] });
    return response({ items: [{ id: 'thread-1', title: '合成对话' }] });
  }));
  render(<Workbench projectId="alpha" threadId="thread-1"/>); await act(async () => {});
  fireEvent.click(screen.getByRole('button', { name: '打开成果' })); await act(async () => {});
  expect(screen.getByRole('dialog', { name: '成果' })).toBeInTheDocument(); source();
  fireEvent.change(screen.getByRole('textbox', { name: '成果正文' }), { target: { value: '# 修订成果' } });
  fireEvent.click(screen.getByRole('button', { name: '保存' })); await act(async () => {});
  const save = fetch.mock.calls.find(([, options]) => options.method === 'PATCH');
  expect(save[0]).toBe('/api/recognition/documents/synthetic-doc');
  expect(JSON.parse(save[1].body)).toEqual({ project_id: 'alpha', expected_revision: 4, markdown: '# 修订成果' });
});
