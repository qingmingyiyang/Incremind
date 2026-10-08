import { useState } from 'react';
import { act, cleanup, fireEvent, render, screen } from '@testing-library/react';
import { afterEach, expect, it, vi } from 'vitest';
import { TextSelection } from '@src/../node_modules/prosemirror-state/dist/index.js';
import { EditorView } from '@src/../node_modules/prosemirror-view/dist/index.js';
import { RichMarkdownEditor } from '@src/shared/ui/RichMarkdownEditor';
import { SourceDraftView } from '@src/shared/ui/SourceDraftView';
import { splitMarkdown } from '@src/shared/ui/richMarkdown';

afterEach(cleanup);
function Harness({ initial, onSave = () => {}, evidence = [], onEvidence, disabled }) {
  const [value, setValue] = useState(initial);
  return <RichMarkdownEditor value={value} onChange={setValue} onSave={onSave} evidence={evidence} onEvidence={onEvidence} disabled={disabled}/>;
}
function activate(text) {
  const calls = vi.spyOn(EditorView.prototype, 'setProps'); fireEvent.click(screen.getByText(text));
  const view = calls.mock.contexts.find(instance => instance.dom.getAttribute('contenteditable') === 'true'); calls.mockRestore();
  expect(view).toBeInstanceOf(EditorView); return view;
}
function selectAll(view) { act(() => view.dispatch(view.state.tr.setSelection(TextSelection.create(view.state.doc, 1, view.state.doc.firstChild.nodeSize - 1)))); }
it('edits a rendered paragraph and saves via Ctrl+S without touching its neighbors', () => {
  const save = vi.fn(), original = '# 标题\r\n\r\n要编辑。\r\n\r\n- [ ] 不改变待办\r\n';
  render(<Harness initial={original} onSave={save}/>);
  const view = activate('要编辑。');
  act(() => view.dispatch(view.state.tr.insertText('已修订', 1, 4)));
  fireEvent.keyDown(view.dom, { key: 's', ctrlKey: true });
  expect(save).toHaveBeenCalledWith(original.replace('要编辑。', '已修订。'));
  expect(save).toHaveBeenCalledTimes(1);
});
it('shows icon-only formatting on selection and uses actual Ctrl+B and Ctrl+I', () => {
  const save = vi.fn(); render(<Harness initial="selected" onSave={save}/>);
  const view = activate('selected'); selectAll(view);
  expect(screen.getByRole('toolbar', { name: '文字格式' })).toBeInTheDocument();
  for (const button of screen.getByRole('toolbar', { name: '文字格式' }).querySelectorAll('button')) { expect(button.textContent).toBe(''); expect(button.querySelector('[data-icon]')).not.toBeNull(); }
  fireEvent.keyDown(view.dom, { key: 'b', code: 'KeyB', ctrlKey: true });
  fireEvent.keyDown(view.dom, { key: 'i', code: 'KeyI', ctrlKey: true });
  expect(view.dom.querySelector('strong em,em strong')).not.toBeNull();
  fireEvent.click(screen.getByRole('button', { name: '保存' })); expect(save.mock.calls[0][0]).toContain('selected');
});
it('opens a link editor via Ctrl+K and serializes the selected text', () => {
  const save = vi.fn(); render(<Harness initial="selected" onSave={save}/>);
  const view = activate('selected'); selectAll(view);
  fireEvent.keyDown(view.dom, { key: 'k', code: 'KeyK', ctrlKey: true });
  fireEvent.change(screen.getByRole('textbox', { name: '链接地址' }), { target: { value: 'https://example.invalid/a' } });
  fireEvent.click(screen.getByRole('button', { name: '应用链接' }));
  fireEvent.click(screen.getByRole('button', { name: '保存' })); expect(save).toHaveBeenCalledWith('[selected](https://example.invalid/a)');
});
it('reopens Chinese Ctrl+B/I/K formatting through source with the complete text, marks and original neighbor bytes', () => {
  const body = '只改这一个段落，旁边所有块与待办文字保持原样。😀 Q51单段已改。';
  const prefix = '# 原标题\r\n\r\n前段保持。\r\n\r\n', tail = '\r\n\r\n## 待办\r\n* [X] 已登记\r\n+ [ ] 准备原件。';
  const href = 'https://example.invalid/q51-edited', original = prefix + body + tail, save = vi.fn();
  render(<Harness initial={original} onSave={save}/>); const view = activate(body);
  const from = 1 + body.lastIndexOf('改。'), to = from + 2;
  act(() => view.dispatch(view.state.tr.setSelection(TextSelection.create(view.state.doc, from, to))));
  fireEvent.keyDown(view.dom, { key: 'b', code: 'KeyB', ctrlKey: true });
  fireEvent.keyDown(view.dom, { key: 'i', code: 'KeyI', ctrlKey: true });
  fireEvent.keyDown(view.dom, { key: 'k', code: 'KeyK', ctrlKey: true });
  fireEvent.change(screen.getByRole('textbox', { name: '链接地址' }), { target: { value: href } });
  fireEvent.click(screen.getByRole('button', { name: '应用链接' }));
  const expectedDoc = view.state.doc, expected = prefix + body.slice(0, -2) + `[***改。***](${href})` + tail;
  fireEvent.keyDown(view.dom, { key: 's', code: 'KeyS', ctrlKey: true });
  expect(save).toHaveBeenLastCalledWith(expected);
  fireEvent.click(screen.getByRole('button', { name: '更多编辑' })); fireEvent.click(screen.getByRole('button', { name: '源码' }));
  expect(screen.getByRole('textbox', { name: '整理稿正文' })).toHaveValue(expected.replaceAll('\r\n', '\n'));
  const index = splitMarkdown(original).findIndex(block => block.raw === body);
  // Textarea DOM normalizes CRLF. The actual saved state and parsed block still use the original bytes.
  expect(splitMarkdown(save.mock.calls[0][0])[index].doc.eq(expectedDoc)).toBe(true);
  fireEvent.click(screen.getByRole('button', { name: '保存' })); expect(save).toHaveBeenLastCalledWith(expected);
  fireEvent.click(screen.getByRole('button', { name: '更多编辑' }));
  const props = vi.spyOn(EditorView.prototype, 'setProps');
  fireEvent.click(screen.getByRole('button', { name: '整理稿' }));
  const reopenedView = props.mock.contexts.find(instance => instance.state.doc.textContent === body); props.mockRestore();
  expect(reopenedView).toBeInstanceOf(EditorView);
  expect(reopenedView.state.doc.eq(expectedDoc)).toBe(true);
  const reopened = reopenedView.dom, anchor = reopened.querySelector('a');
  expect(reopened.textContent).toBe(body);
  expect(anchor.textContent).toBe('改。'); expect(anchor.getAttribute('href')).toBe(href);
  expect(anchor.querySelector('strong')).not.toBeNull(); expect(anchor.querySelector('em')).not.toBeNull();
  fireEvent.click(screen.getByRole('button', { name: '保存' }));
  expect(save).toHaveBeenLastCalledWith(expected);
  expect([...new TextEncoder().encode(save.mock.calls.at(-1)[0])]).toEqual([...new TextEncoder().encode(expected)]);
});
it('switches to source and back without rewriting CRLF or losing edits', () => {
  const save = vi.fn(), markdown = '# 标题\r\n\r\n原文。\r\n'; render(<Harness initial={markdown} onSave={save}/>);
  fireEvent.click(screen.getByRole('button', { name: '更多编辑' })); fireEvent.click(screen.getByRole('button', { name: '源码' }));
  fireEvent.click(screen.getByRole('button', { name: '保存' })); expect(save).toHaveBeenLastCalledWith(markdown);
  fireEvent.change(screen.getByRole('textbox', { name: '整理稿正文' }), { target: { value: '# 新标题\n\n新正文。' } });
  fireEvent.click(screen.getByRole('button', { name: '更多编辑' })); fireEvent.click(screen.getByRole('button', { name: '整理稿' }));
  expect(screen.getByRole('heading', { name: '新标题' })).toBeInTheDocument();
  fireEvent.click(screen.getByRole('button', { name: '保存' })); expect(save).toHaveBeenLastCalledWith('# 新标题\n\n新正文。');
});
it('allows task text editing but provides no status or formatting controls', () => {
  const save = vi.fn(); render(<Harness initial="- [X] 合成待办" onSave={save}/>);
  const view = activate('合成待办');
  act(() => view.dispatch(view.state.tr.insertText('已改', 3, 5)));
  fireEvent.click(screen.getByRole('button', { name: '保存' }));
  expect(save.mock.calls[0][0]).toContain('[X] 已改待办');
  expect(screen.queryByRole('checkbox')).not.toBeInTheDocument(); expect(screen.queryByRole('toolbar', { name: '文字格式' })).not.toBeInTheDocument();
});
it('renders split ordered numbering and nested lists while preserving task markers and source bytes', () => {
  const original = '7. 甲项\r\n   - 子项\r\n\r\n     4. 内层数字\r\n8. 乙项\r\n\r\n## 待办\r\n* [X] 不动待办', save = vi.fn();
  const { container } = render(<Harness initial={original} onSave={save}/>);
  const lists = container.querySelectorAll('.ProseMirror > ol');
  expect([...lists].map(list => list.getAttribute('start'))).toEqual(['7', '8']);
  expect(lists[0].querySelectorAll(':scope > li')).toHaveLength(1);
  const bullet = lists[0].querySelector('li > ul');
  expect(bullet.querySelectorAll(':scope > li')).toHaveLength(1);
  expect(bullet.querySelector('li > ol')).toHaveAttribute('start', '4');
  const task = container.querySelector('li[data-task-state]');
  expect(task).toHaveAttribute('data-task-state', 'X');
  expect(task.querySelector('[aria-label="待办"]')).toHaveTextContent('●');
  expect(screen.queryByRole('checkbox')).not.toBeInTheDocument();
  fireEvent.click(screen.getByRole('button', { name: '保存' }));
  expect(save).toHaveBeenCalledExactlyOnceWith(original);
});
it('keeps remote images and HTML inert in the rendered and active editor', () => {
  const { container } = render(<Harness initial={'![私密图](https://example.invalid/image)\n\n<script>bad()</script>\n\n[坏链接](javascript:bad)'}/>);
  expect(container.querySelector('img[src],script,a[href^="javascript:"]')).toBeNull();
  activate('私密图'); expect(container.querySelector('img[src],script')).toBeNull();
});
it('preserves SourceDraftView defaults and opt-in original evidence highlighting', () => {
  const fact = { text: '合成事实', evidence: { start: 0, end: 2, quote: '原句' } };
  const view = render(<SourceDraftView source="原句后文" draft={{ title: '标题', facts: [fact] }} highlightFacts/>);
  expect(screen.getByRole('button', { name: '定位原文：合成事实' })).toBeInTheDocument();
  view.rerender(<SourceDraftView source="原句后文" draft={{ title: '标题', facts: [fact] }} highlightFacts renderedEditor={({ locateFact }) => <Harness initial="- 合成事实" evidence={[fact]} onEvidence={locateFact}/>}/>);
  expect(screen.queryByText('标题')).not.toBeInTheDocument();
  fireEvent.click(screen.getByRole('button', { name: '定位原文：合成事实' }));
  expect(view.container.querySelector('mark[data-current=true]')).toHaveTextContent('原句');
});
it('removes evidence navigation after the fact text changes and rejects duplicate matches', () => {
  const fact = { text: '合成事实', evidence: { quote: '原句', start: 0, end: 2 } }, open = vi.fn();
  const view = render(<Harness initial="- 合成事实" evidence={[fact]} onEvidence={open}/>);
  const editor = activate('合成事实'); act(() => editor.dispatch(editor.state.tr.insertText('新', 3, 4)));
  expect(screen.queryByRole('button', { name: '定位原文：合成事实' })).not.toBeInTheDocument();
  view.rerender(<Harness initial="- 合成事实" evidence={[fact, fact]} onEvidence={open}/>);
  expect(screen.queryByRole('button', { name: '定位原文：合成事实' })).not.toBeInTheDocument(); expect(open).not.toHaveBeenCalled();
});
it('does not offer a location when a frozen fact is absent from the editable Markdown', () => {
  render(<Harness initial="实际正文" evidence={[{ text: '没有出现在正文', evidence: { start: 0, end: 2, quote: '原句' } }]} onEvidence={() => {}}/>);
  expect(screen.queryByRole('button', { name: '定位原文：没有出现在正文' })).not.toBeInTheDocument();
});
it('does not choose evidence for two identical visible fact blocks', () => {
  render(<Harness initial={'- 合成事实\n- 合成事实'} evidence={[{ text: '合成事实', evidence: { start: 0, end: 2, quote: '原句' } }]} onEvidence={() => {}}/>);
  expect(screen.queryByRole('button', { name: '定位原文：合成事实' })).not.toBeInTheDocument();
});
it('uses plus and minus controls at table edges to edit rows and columns', () => {
  const save = vi.fn(), view = render(<Harness initial={'| a | b |\n| --- | --- |\n| c | d |'} onSave={save}/>);
  fireEvent.mouseOver(screen.getByText('c').closest('td'));
  fireEvent.click(screen.getByRole('button', { name: '增加行' }));
  expect(view.container.querySelectorAll('tr')).toHaveLength(3);
  fireEvent.click(screen.getByRole('button', { name: '增加列' }));
  expect(view.container.querySelector('tr').children).toHaveLength(3);
  fireEvent.click(screen.getByRole('button', { name: '保存' })); expect(save.mock.calls[0][0]).toContain('| c |');
});

it('preserves CRLF and task bytes with a count widget and excludes the widget from formatting and serialization', () => {
  const prefix = '# 😀 原稿\r\n\r\n', comment = '## 评论区', tail = '\r\n\r\n~~~\r\n甲评论\r\n~~~\r\n\r\n* [X] 不改变待办';
  const initial = prefix + comment + tail, save = vi.fn(), calls = vi.spyOn(EditorView.prototype, 'setProps');
  const section = { document_id: 'synthetic-count', document_revision: 4, coordinate_space: 'document_markdown_v1', count: 1,
    heading: { start: [...prefix].length, end: [...prefix].length + comment.length } };
  function Counted() {
    const [value, setValue] = useState(initial);
    return <RichMarkdownEditor value={value} onChange={setValue} onSave={save} documentId="synthetic-count"
      documentRevision={4} documentMarkdown={initial} commentSection={section}/>;
  }
  const view = render(<Counted/>);
  expect(view.container.querySelector('.ProseMirror h2 > .ui-comment-count')).toHaveTextContent('1');
  const editor = calls.mock.contexts.find(instance => instance.state.doc.firstChild?.type.name === 'heading' && instance.state.doc.textContent === '评论区');
  fireEvent.click(editor.dom); selectAll(editor);
  fireEvent.keyDown(editor.dom, { key: 'b', code: 'KeyB', ctrlKey: true });
  expect(view.container.querySelector('.ui-comment-count')).toBeNull();
  expect(editor.state.doc.textContent).toBe('评论区');
  fireEvent.keyDown(editor.dom, { key: 's', code: 'KeyS', ctrlKey: true });
  expect(save).toHaveBeenLastCalledWith(prefix + '## **评论区**' + tail);
  expect(splitMarkdown(save.mock.calls[0][0]).find(block => block.start === prefix.length).doc.eq(editor.state.doc)).toBe(true);
  calls.mockRestore();
});
