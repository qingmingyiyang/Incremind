import { act, cleanup, render, screen } from '@testing-library/react';
import { afterEach, expect, it, vi } from 'vitest';
import { WorkbenchDraftPanel } from '@src/features/workbench/WorkbenchDraftPanel';

afterEach(() => { cleanup(); vi.unstubAllGlobals(); });
it('opens the draft as editable rendered blocks instead of a Markdown textarea', async () => {
  const markdown = '# 合成稿\n\n## 摘要\n摘要，保留标点。\n\n一段可直接编辑的正文。';
  vi.stubGlobal('fetch', vi.fn(async () => ({ ok: true, status: 200, json: async () => ({
    note: { document_id: 'synthetic', title: '合成稿', revision: 3, markdown, facts: [], todos: [] },
    summary: { text: '摘要，保留标点。' }, sources: [],
  }) })));
  render(<WorkbenchDraftPanel projectId="synthetic" documentId="synthetic" onClose={() => {}}/>);
  await act(async () => {});
  expect(screen.queryByRole('textbox', { name: '整理稿正文' })).not.toBeInTheDocument();
  expect(screen.getByRole('heading', { name: '合成稿', level: 1 })).toBeInTheDocument();
  expect(screen.getByRole('button', { name: '更多编辑' })).toBeInTheDocument();
});
