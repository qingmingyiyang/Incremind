import { useState } from 'react';
import { act, cleanup, fireEvent, render, screen, within } from '@testing-library/react';
import { afterEach, expect, it, vi } from 'vitest';
import { EditorView } from '@src/../node_modules/prosemirror-view/dist/index.js';
import { MarkdownBody } from '@src/shared/ui/MarkdownBody';
import { RichMarkdownEditor } from '@src/shared/ui/RichMarkdownEditor';
import { CandidateHint } from '@src/shared/ui/CandidateHint';
import { InsightChip } from '@src/shared/ui/InsightChip';
import { DocumentMarkdownEditor } from '@src/shared/ui/DocumentMarkdownEditor';
import { WorkbenchDraftPanel } from '@src/features/workbench/WorkbenchDraftPanel';
import { WorkbenchOutcomePanel } from '@src/features/workbench/WorkbenchOutcomePanel';
import { Library } from '@src/features/library/Library';

afterEach(() => { cleanup(); vi.unstubAllGlobals(); vi.restoreAllMocks(); });
// Independent synthetic projection with the exact capture/confirm DTO contract.
// The first title and marker are ordinary body text, never display authority.
const base = '# 整理稿\n\n## 摘要\n\n😀\n\n## 评论区\n\n伪造的正文\n';
const markdown = base + '\n\n## 评论区\n\n<!-- source_sections.comments: capture-qualified -->\n\n### 第1条 · 20 赞\n\n~~~\n周末已经关门 😀\n~~~\n\n### 第2条 · 10 赞\n\n~~~\n记得预约\n~~~';
const headingStart = [...base].length + 2;
const section = { document_id: 'synthetic-note', document_revision: 4,
  coordinate_space: 'document_markdown_v1', count: 2,
  heading: { start: headingStart, end: headingStart + 6 } };
const note = { document_id: 'synthetic-note', title: '整理稿', revision: 4,
  markdown, facts: [], todos: [], verified: false, comment_section: section };
const commentSource = { type: 'original_item', id: 'synthetic-original', project_id: 'alpha', revision: 6,
  coordinate_space: 'workspace_source_text_v1', ordinal: 1, start: 170, end: 178, quote: '周末已经关门 😀' };
const pending = { id: 'candidate-one', revision: 1, state: 'pending', kind: 'candidate', text: '周末先确认营业时间',
  conditions: ['周末'], hint: { relation: 'differs', target_id: null, scope_hint: null, target: null }, comment_source: commentSource };
const response = (value, status = 200) => ({ ok: status < 400, status, json: async () => value, text: async () => JSON.stringify(value) });
function counts(container) { return [...container.querySelectorAll('.ui-comment-count')]; }
function source(label = '整理稿正文') {
  fireEvent.click(screen.getByRole('button', { name: '更多编辑' })); fireEvent.click(screen.getByRole('button', { name: '源码' }));
  return screen.getByRole('textbox', { name: label });
}
function Harness({ onSave }) {
  const [value, setValue] = useState(markdown);
  return <RichMarkdownEditor value={value} onChange={setValue} onSave={onSave}
    documentId={note.document_id} documentRevision={4} documentMarkdown={markdown} commentSection={section}/>;
}
function setup({ readonly = false, conflict = false } = {}) {
  let current = structuredClone(note);
  vi.stubGlobal('fetch', vi.fn(async (url, options = {}) => {
    const path = new URL(String(url), 'http://localhost').pathname;
    if (path.endsWith('/drill')) return response({ note: current, readonly, sources: [] });
    if (path.endsWith('/documents/synthetic-note') && options.method === 'PATCH') {
      if (conflict) {
        conflict = false; current = { ...note, revision: 7, comment_section: { ...section, document_revision: 7 } };
        return response({ detail: 'revision_conflict' }, 409);
      }
      const body = JSON.parse(options.body); const { comment_section: _old, ...fields } = current;
      current = { ...fields, revision: current.revision + 1, markdown: body.markdown };
      return response({ id: current.document_id, revision: current.revision });
    }
    if (path.endsWith('/documents/synthetic-note')) return response({ ...current, id: current.document_id });
    if (path.endsWith('/consolidate')) return response({ score: 7 });
    return response({ items: path.endsWith('/notes') ? [current] : path.endsWith('/insights') ? [pending] : [], counts: {} });
  }));
}

it('adds the count only to the owner-projected heading after a codepoint-to-UTF16 conversion', () => {
  const view = render(<MarkdownBody documentId={note.document_id} documentRevision={4} commentSection={section}>{markdown}</MarkdownBody>);
  const headings = view.container.querySelectorAll('h2');
  expect(headings[1].textContent).toBe('评论区');
  expect(headings[2].querySelector('.ui-comment-count')).toHaveTextContent('2');
  expect(counts(view.container)).toHaveLength(1);
  expect(headings[2].lastElementChild).toHaveClass('ui-comment-count');
  expect(view.container.querySelector('img,script')).toBeNull();
});

it.each([
  undefined, '评论区', { ...section, count: true }, { ...section, count: 0 },
  { ...section, document_id: 'other-note' }, { ...section, document_revision: 3 },
  { ...section, coordinate_space: 'workspace_source_text_v1' },
  { ...section, heading: { start: true, end: headingStart + 6 } },
  { ...section, heading: { start: headingStart + 1, end: headingStart + 7 } },
])('hides missing or malformed authority while leaving literal headings and markers readable (%j)', commentSection => {
  const view = render(<MarkdownBody documentId={note.document_id} documentRevision={4} commentSection={commentSection}>{markdown}</MarkdownBody>);
  expect(counts(view.container)).toHaveLength(0);
  expect(view.container.querySelectorAll('h2')).toHaveLength(3);
  expect(screen.getByText('伪造的正文')).toBeInTheDocument();
});

it('uses a noneditable widget in the real editor and keeps it out of state, source and saved bytes', () => {
  const save = vi.fn(), props = vi.spyOn(EditorView.prototype, 'setProps');
  const view = render(<Harness onSave={save}/>);
  const widget = counts(view.container)[0]; expect(widget).toHaveTextContent('2');
  expect(widget).toHaveAttribute('contenteditable', 'false');
  const editor = props.mock.contexts.find(instance => instance.dom.contains(widget));
  expect(editor.state.doc.textContent).toBe('评论区');
  fireEvent.click(screen.getByRole('button', { name: '保存' })); expect(save).toHaveBeenCalledExactlyOnceWith(markdown);
  expect(source()).toHaveValue(markdown);
  fireEvent.click(screen.getByRole('button', { name: '更多编辑' })); fireEvent.click(screen.getByRole('button', { name: '整理稿' }));
  expect(counts(view.container)).toHaveLength(1);
  const call = props.mock.contexts.find(instance => instance.dom.isConnected && instance.state.doc.textContent === '伪造的正文');
  act(() => call.dispatch(call.state.tr.insertText('人工', 1, 3)));
  expect(counts(view.container)).toHaveLength(0);
  fireEvent.click(screen.getByRole('button', { name: '保存' }));
  expect(save).toHaveBeenLastCalledWith(markdown.replace('伪造', '人工'));
});

const systemMarker = '<!-- source_sections.comments: capture-qualified -->';
it('hides only the standalone system marker in the reader without shifting the projected count', () => {
  const view = render(<MarkdownBody documentId={note.document_id} documentRevision={4} commentSection={section}>{markdown}</MarkdownBody>);
  expect(view.container.textContent).not.toContain(systemMarker);
  expect(counts(view.container)).toHaveLength(1);
  expect(counts(view.container)[0]).toHaveTextContent('2');
  expect(screen.getByText('伪造的正文')).toBeInTheDocument();
});

it.each(['\n', '\r\n'])('hides the standalone system marker while preserving real editor source and saved bytes (%j)', newline => {
  const original = markdown.replace(/\n/g, newline);
  const start = [...original.slice(0, original.lastIndexOf('## 评论区'))].length;
  const projection = { ...section, heading: { start, end: start + 6 } };
  const save = vi.fn(), props = vi.spyOn(EditorView.prototype, 'setProps');
  function MarkerEditor() {
    const [value, setValue] = useState(original);
    return <RichMarkdownEditor value={value} onChange={setValue} onSave={save}
      documentId={note.document_id} documentRevision={4} documentMarkdown={original} commentSection={projection}/>;
  }
  const view = render(<MarkerEditor/>);
  expect(view.container.textContent).not.toContain(systemMarker);
  expect(counts(view.container)).toHaveLength(1);
  expect(counts(view.container)[0]).toHaveTextContent('2');
  fireEvent.click(screen.getByRole('button', { name: '保存' }));
  expect(save).toHaveBeenLastCalledWith(original);
  expect(source()).toHaveValue(original.replace(/\r\n/g, '\n'));
  fireEvent.click(screen.getByRole('button', { name: '保存' }));
  expect(save).toHaveBeenLastCalledWith(original);
  fireEvent.click(screen.getByRole('button', { name: '更多编辑' }));
  fireEvent.click(screen.getByRole('button', { name: '整理稿' }));
  expect(view.container.textContent).not.toContain(systemMarker);
  const editor = props.mock.contexts.find(instance => instance.dom.isConnected && instance.state.doc.textContent === '伪造的正文');
  act(() => editor.dispatch(editor.state.tr.insertText('人工', 1, 3)));
  expect(counts(view.container)).toHaveLength(0);
  fireEvent.click(screen.getByRole('button', { name: '保存' }));
  expect(save).toHaveBeenLastCalledWith(original.replace('伪造', '人工'));
});

it.each(['reader', 'editor'])('keeps literal code markers and ordinary HTML comments while hiding multiple standalone system markers (%s)', mode => {
  const ordinary = '<!-- 用户备注 -->';
  const value = `前文\n\n${systemMarker}\n\n相邻正文\n\n${systemMarker}\n\n${ordinary}\n\n\`${systemMarker}\`\n\n~~~\n${systemMarker}\n~~~\n\n后文`;
  const view = render(mode === 'reader' ? <MarkdownBody>{value}</MarkdownBody> : <RichMarkdownEditor value={value}/>);
  expect(view.container.textContent.split(systemMarker)).toHaveLength(3);
  expect(view.container.textContent).toContain(ordinary);
  expect(screen.getByText('相邻正文')).toBeInTheDocument();
  expect(screen.getByText('前文')).toBeInTheDocument();
  expect(screen.getByText('后文')).toBeInTheDocument();
  expect(view.container.querySelectorAll('code')).toHaveLength(2);
});

it('places the proven pending comment mark immediately after its relation icon in both shared components', () => {
  const view = render(<><CandidateHint relation="differs" commentSource={commentSource}/><InsightChip insight={pending}/></>);
  expect(screen.getAllByText('评')).toHaveLength(2);
  for (const hint of view.container.querySelectorAll('.candidate-hint')) {
    expect(hint.firstElementChild).toHaveAttribute('data-icon', 'differs');
    expect(hint.lastElementChild).toHaveTextContent('评');
  }
});

it('marks a proven comment against a neighboring recognition with the existing may_supersede relation', () => {
  const candidate = { ...pending, hint: { ...pending.hint, relation: 'may_supersede', target_id: 'neighbor-one' } };
  const view = render(<InsightChip insight={candidate}/>);
  const hint = view.container.querySelector('.candidate-hint');
  expect(hint.firstElementChild).toHaveAttribute('data-icon', 'may_supersede');
  expect(hint.lastElementChild).toHaveClass('ui-comment-origin'); expect(hint.lastElementChild).toHaveTextContent('评');
});

it.each([undefined, '评论区', true, { ...commentSource, ordinal: true },
  { ...commentSource, coordinate_space: 'document_markdown_v1' }, { ...commentSource, end: 171 }])(
  'does not gain a comment mark from words or malformed evidence (%j)', proof => {
    render(<><CandidateHint relation="supplement" commentSource={proof}/><InsightChip insight={{ ...pending, text: '评 评论区', comment_source: proof }}/></>);
    expect(document.querySelector('.ui-comment-origin')).toBeNull();
  });

it('does not mark an active insight even when it retains an old pending projection', () => {
  render(<InsightChip insight={{ ...pending, state: 'active' }}/>);
  expect(document.querySelector('.ui-comment-origin')).toBeNull();
});

it.each(['remember', 'outcome', 'library', 'readonly'])('connects the real %s entry to the qualified projection', async entry => {
  setup({ readonly: entry === 'readonly' });
  const view = render(entry === 'remember' ? <WorkbenchDraftPanel projectId="alpha" documentId={note.document_id} onClose={() => {}}/>
    : entry === 'outcome' ? <WorkbenchOutcomePanel projectId="alpha" documentId={note.document_id} document={{ id: note.document_id, revision: 4, markdown }} onClose={() => {}}/>
      : <Library projectId="alpha" initialLayer="note" documentId={note.document_id}/>);
  await act(async () => {});
  expect(counts(view.container)).toHaveLength(1);
  expect(counts(view.container)[0]).toHaveTextContent('2');
  if (entry === 'readonly') { expect(screen.queryByRole('button', { name: '保存' })).not.toBeInTheDocument(); return; }
  source(entry === 'outcome' ? '成果正文' : '整理稿正文');
  fireEvent.click(screen.getByRole('button', { name: '保存' })); await act(async () => {});
  const saves = fetch.mock.calls.filter(([, options]) => options.method === 'PATCH');
  expect(saves).toHaveLength(1);
  expect(JSON.parse(saves[0][1].body)).toEqual({ project_id: 'alpha', expected_revision: 4, markdown });
});

it('projects comment evidence onto the real library pending row', async () => {
  setup(); const view = render(<Library projectId="alpha"/>); await act(async () => {});
  expect(view.container.querySelector('.library-rows .ui-comment-origin')).toHaveTextContent('评');
  expect(view.container.querySelector('.library-rows [data-icon="differs"]')).not.toBeNull();
});

it('reloads count through the real 409 read model and retains all snapshots and server CAS', async () => {
  setup({ conflict: true }); const view = render(<Library projectId="alpha" initialLayer="note" documentId={note.document_id}/>); await act(async () => {});
  const input = source(); fireEvent.change(input, { target: { value: '本地正文' } });
  fireEvent.click(screen.getByRole('button', { name: '保存' })); await act(async () => {});
  const alert = screen.getByRole('alert', { name: '草稿版本冲突' });
  expect(alert.textContent).toContain(markdown); expect(alert.textContent).toContain('本地正文');
  expect(screen.getByRole('button', { name: '保存' })).toBeDisabled();
  fireEvent.click(screen.getByRole('button', { name: '采用服务器版本' }));
  const editor = within(view.container.querySelector('.ui-rich-editor'));
  fireEvent.click(editor.getByRole('button', { name: '更多编辑' })); fireEvent.click(editor.getByRole('button', { name: '整理稿' }));
  expect(counts(view.container)).toHaveLength(1);
  fireEvent.click(screen.getByRole('button', { name: '保存' })); await act(async () => {});
  const saves = fetch.mock.calls.filter(([, options]) => options.method === 'PATCH');
  expect(JSON.parse(saves[1][1].body)).toEqual({ project_id: 'alpha', expected_revision: 7, markdown });
  expect(counts(view.container)).toHaveLength(0);
});

it('rejects a late comment read after the project and document scope changes', async () => {
  let finish; vi.stubGlobal('fetch', vi.fn(url => String(url).includes('project_id=alpha')
    ? new Promise(resolve => { finish = resolve; }) : Promise.resolve(response({ note: { ...note, document_id: 'beta-note', markdown: '# 乙稿', comment_section: undefined } }))));
  const view = render(<WorkbenchOutcomePanel projectId="alpha" documentId={note.document_id} document={{ id: note.document_id, revision: 4, markdown }} onClose={() => {}}/>);
  await act(async () => {});
  view.rerender(<WorkbenchOutcomePanel projectId="beta" documentId="beta-note" document={{ id: 'beta-note', revision: 4, markdown: '# 乙稿' }} onClose={() => {}}/>);
  await act(async () => {});
  expect(typeof finish).toBe('function');
  await act(async () => { finish(response({ note })); });
  expect(screen.getByRole('heading', { name: '乙稿' })).toBeInTheDocument(); expect(counts(view.container)).toHaveLength(0);
});

it('does not restore an old revision from a late initial projection after a successful CAS save', async () => {
  let finish, reads = 0;
  vi.stubGlobal('fetch', vi.fn(async () => response({ id: note.document_id, revision: 5 })));
  const readDocument = () => ++reads === 1 ? new Promise(resolve => { finish = resolve; })
    : Promise.resolve({ ...note, revision: 5, comment_section: undefined });
  const view = render(<DocumentMarkdownEditor projectId="alpha" documentId={note.document_id} document={note} readDocument={readDocument}/>);
  await act(async () => {}); fireEvent.click(screen.getByRole('button', { name: '保存' })); await act(async () => {});
  expect(counts(view.container)).toHaveLength(0);
  await act(async () => { finish(note); });
  expect(counts(view.container)).toHaveLength(0);
  fireEvent.click(screen.getByRole('button', { name: '保存' })); await act(async () => {});
  expect(JSON.parse(fetch.mock.calls[1][1].body)).toEqual({ project_id: 'alpha', expected_revision: 5, markdown });
});

it('hides a superseded revision projection without adopting its body or advancing the local save CAS', async () => {
  const server = { ...note, revision: 7, comment_section: { ...section, document_revision: 7 } };
  vi.stubGlobal('fetch', vi.fn(async (_url, options = {}) => options.method === 'PATCH'
    ? response({ detail: 'revision_conflict' }, 409) : response({ id: note.document_id, revision: 7, markdown })));
  const view = render(<DocumentMarkdownEditor projectId="alpha" documentId={note.document_id} document={note} readDocument={() => Promise.resolve(server)}/>);
  await act(async () => {});
  expect(counts(view.container)).toHaveLength(0); expect(source()).toHaveValue(markdown);
  fireEvent.click(screen.getByRole('button', { name: '保存' })); await act(async () => {});
  expect(JSON.parse(fetch.mock.calls[0][1].body)).toEqual({ project_id: 'alpha', expected_revision: 4, markdown });
  expect(screen.getByRole('alert', { name: '草稿版本冲突' })).toBeInTheDocument();
});

it('keeps an edited body intact when an optional projection read fails after the save', async () => {
  vi.stubGlobal('fetch', vi.fn(async () => response({ id: note.document_id, revision: 5 })));
  const saved = vi.fn(), readDocument = () => Promise.reject(new Error('offline'));
  const view = render(<DocumentMarkdownEditor projectId="alpha" documentId={note.document_id} document={note} readDocument={readDocument} onSaved={saved}/>);
  await act(async () => {});
  const input = source(); fireEvent.change(input, { target: { value: '已保存正文' } });
  fireEvent.click(screen.getByRole('button', { name: '保存' })); await act(async () => {});
  expect(input).toHaveValue('已保存正文'); expect(counts(view.container)).toHaveLength(0);
  expect(screen.queryByRole('alert')).not.toBeInTheDocument(); expect(saved).toHaveBeenCalledTimes(1);
  expect(saved.mock.calls[0][0]).toMatchObject({ revision: 5, markdown: '已保存正文' });
  expect(saved.mock.calls[0][0]).not.toHaveProperty('comment_section');
});

it('retains the original conflict read when optional comment projection is unavailable', async () => {
  const server = { id: note.document_id, revision: 7, markdown: '服务器正文' };
  vi.stubGlobal('fetch', vi.fn(async (_url, options = {}) => options.method === 'PATCH'
    ? response({ detail: 'revision_conflict' }, 409) : response(server)));
  render(<DocumentMarkdownEditor projectId="alpha" documentId={note.document_id} document={note}
    readDocument={() => Promise.reject(new Error('optional projection unavailable'))}/>);
  await act(async () => {});
  const input = source(); fireEvent.change(input, { target: { value: '本地正文' } });
  fireEvent.click(screen.getByRole('button', { name: '保存' })); await act(async () => {});
  const alert = screen.getByRole('alert', { name: '草稿版本冲突' });
  expect(alert.textContent).toContain(markdown); expect(alert).toHaveTextContent('服务器正文'); expect(alert).toHaveTextContent('本地正文');
  const read = fetch.mock.calls.find(([, options]) => options.method !== 'PATCH');
  expect(read[0]).toBe('/api/recognition/documents/synthetic-note?project_id=alpha');
  expect(screen.getByRole('button', { name: '保存' })).toBeDisabled();
});

it('finishes a successful save while optional comment projection remains pending', async () => {
  vi.stubGlobal('fetch', vi.fn(async () => response({ id: note.document_id, revision: 5 })));
  let finish, reads = 0;
  const saved = vi.fn(), readDocument = () => ++reads === 1 ? Promise.resolve(note)
    : new Promise(resolve => { finish = resolve; });
  const view = render(<DocumentMarkdownEditor projectId="alpha" documentId={note.document_id} document={note} readDocument={readDocument} onSaved={saved}/>);
  await act(async () => {});
  fireEvent.click(screen.getByRole('button', { name: '保存' })); await act(async () => {});
  expect(saved).toHaveBeenCalledTimes(1);
  expect(saved.mock.calls[0][0]).toMatchObject({ revision: 5, markdown });
  expect(screen.getByRole('button', { name: '保存' })).toBeEnabled(); expect(counts(view.container)).toHaveLength(0);
  await act(async () => { finish({ ...note, revision: 5, comment_section: undefined }); });
  expect(saved).toHaveBeenCalledTimes(1);
});
