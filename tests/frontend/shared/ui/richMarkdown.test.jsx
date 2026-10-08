import { TextSelection } from '@src/../node_modules/prosemirror-state/dist/index.js';
import { expect, it } from 'vitest';
import samples from '../../../fixtures/rich_markdown/samples.json';
import { joinMarkdown, richParser, richSchema, richSerializer, splitMarkdown, updateMarkdownBlock } from '@src/shared/ui/richMarkdown';
import { createRichState, richCommand } from '@src/shared/ui/RichMarkdownEditor';

it.each(samples)('preserves every byte when opening and saving $name', ({ markdown }) => {
  const blocks = splitMarkdown(markdown);
  expect(blocks.filter(block => !block.separator).length).toBeGreaterThan(0);
  expect([...new TextEncoder().encode(joinMarkdown(blocks))]).toEqual([...new TextEncoder().encode(markdown)]);
  for (const [index, block] of blocks.entries()) if (block.doc) expect(joinMarkdown(updateMarkdownBlock(blocks, index, block.doc))).toBe(markdown);
});
it('replaces just one CRLF paragraph and preserves task identity inputs and separators', () => {
  const markdown = '# 标题\r\n\r\n\r\n摘要；中文。\r\n\r\n## 待办\r\n* [X] task A\r\n+ [ ] task B\r\n\r\n```js\r\nconst x=1;\r\n```\r\n';
  const blocks = splitMarkdown(markdown), index = blocks.findIndex(block => block.raw === '摘要；中文。');
  let state = createRichState(blocks[index].doc); state = state.apply(state.tr.insertText('修订', 1, 3));
  expect(joinMarkdown(updateMarkdownBlock(blocks, index, state.doc))).toBe(markdown.replace('摘要；中文。', '修订；中文。'));
});
it.each([
  ['lone CR', '# 标题\r\r甲段。\r\r乙段。'],
  ['mixed CR and LF', '# 标题\r\r甲段。\n\n乙段。\n'],
  ['mixed CRLF and CR', '# 标题\r\n\r\n甲段。\r\r乙段。\n'],
  ['CR code block', '# 标题\r\r```\r甲段。\r接续。\r```\r\r乙段。'],
  ['local CRLF code block after CR heading', '# 标题\r\r```\r\n甲段。\r\n接续。\r\n```\r\n\r\n乙段。\n'],
])('preserves untouched original bytes when editing the middle block with %s', (_, markdown) => {
  const blocks = splitMarkdown(markdown), index = blocks.findIndex(block => block.doc?.textContent.includes('甲段'));
  expect(index).toBeGreaterThanOrEqual(0);
  let state = createRichState(blocks[index].doc), position;
  state.doc.descendants((node, from) => { if (node.isText && node.text.includes('甲段')) position = from + node.text.indexOf('甲段'); });
  state = state.apply(state.tr.insertText('甲改', position, position + 2));
  expect([...new TextEncoder().encode(joinMarkdown(updateMarkdownBlock(blocks, index, state.doc)))]).toEqual([...new TextEncoder().encode(markdown.replace('甲段', '甲改'))]);
});
it('uses the edited single-line block terminator for inserted serialized line breaks in a mixed document', () => {
  const markdown = '# 标题\r\r甲段。\n\n乙段。\r';
  const blocks = splitMarkdown(markdown), index = blocks.findIndex(block => block.doc?.textContent === '甲段。');
  expect(index).toBeGreaterThanOrEqual(0);
  let state = createRichState(blocks[index].doc);
  state = state.apply(state.tr.insert(3, richSchema.nodes.hard_break.create()));
  expect(joinMarkdown(updateMarkdownBlock(blocks, index, state.doc))).toBe(markdown.replace('甲段。', '甲段\\\n。'));
});
it('preserves a sibling list item when editing just one item', () => {
  const markdown = '* 甲\n* [ ] 待办不变\n* 乙\n';
  const blocks = splitMarkdown(markdown), index = blocks.findIndex(block => block.raw === '* 甲');
  let state = createRichState(blocks[index].doc); state = state.apply(state.tr.insertText('修改', 3, 4));
  const changed = joinMarkdown(updateMarkdownBlock(blocks, index, state.doc));
  expect(changed).toContain('* [ ] 待办不变\n* 乙\n'); expect(changed).toContain('修改');
});
it('keeps task status markers immutable while allowing task text edits', () => {
  const doc = richParser.parse('- [X] 核对甲\n  - [ ] 子待办');
  let state = createRichState(doc);
  const changed = state.apply(state.tr.insertText('乙', 5, 6));
  expect(changed.doc.textContent).not.toContain('[X]');
  expect(richSerializer.serialize(changed.doc)).toContain('[X]');
  const illegal = changed.tr.setNodeMarkup(1, null, { task: ' ' });
  expect(changed.apply(illegal).doc).toEqual(changed.doc);
  expect(richCommand('bold', changed, () => {})).toBe(false);
});
it('protects ordinary todo bullets in the actual 待办 section even without checkbox syntax', () => {
  const blocks = splitMarkdown('## 待办\n- 联系甲\n\n## 关键事实\n- 普通事实');
  let todo = createRichState(blocks.find(block => block.raw === '- 联系甲').doc);
  expect(richCommand('bold', todo, () => {})).toBe(false);
  todo = todo.apply(todo.tr.insertText('乙', 5, 6));
  expect(todo.doc.textContent).toBe('联系乙');
  expect(richCommand('bold', createRichState(blocks.find(block => block.raw === '- 普通事实').doc), () => {})).toBe(true);
});
function selected(markdown = 'selected text') {
  const doc = richParser.parse(markdown); let state = createRichState(doc);
  state = state.apply(state.tr.setSelection(TextSelection.create(doc, 1, doc.firstChild.nodeSize - 1)));
  return state;
}
it.each([['bold', '**selected text**'], ['italic', '*selected text*'], ['code', '`selected text`'], ['heading-3', '### selected text']])('supports %s on actual ProseMirror state', (command, expected) => {
  let state = selected(); expect(richCommand(command, state, tr => { state = state.apply(tr); })).toBe(true);
  expect(richSerializer.serialize(state.doc)).toBe(expected);
});
it('serializes an edited link', () => {
  let state = selected(); state = state.apply(state.tr.addMark(1, 14, richSchema.marks.link.create({ href: 'https://example.invalid/a' })));
  expect(richSerializer.serialize(state.doc)).toBe('[selected text](https://example.invalid/a)');
});

const emphasisLinks = [
  ['strong', ['bold'], '**'], ['em', ['italic'], '*'], ['strong and em', ['bold', 'italic'], '***'],
];
it.each(emphasisLinks)('reopens a Chinese internal link with %s without changing text or neighboring bytes', (_, commands, delimiter) => {
  const body = '只改这一个段落，旁边所有块与待办文字保持原样。😀 Q51单段已改。';
  const prefix = '# 原标题\r\n\r\n前段保持。\r\n\r\n', tail = '\r\n\r\n## 待办\r\n* [X] 已登记\r\n+ [ ] 准备原件。\r\n\r\n```js\r\nconst x=1;\r\n```';
  const original = prefix + body + tail, href = 'https://example.invalid/q51-edited';
  const blocks = splitMarkdown(original), index = blocks.findIndex(block => block.raw === body);
  let state = createRichState(blocks[index].doc);
  const from = 1 + body.lastIndexOf('改。'), to = from + 2;
  state = state.apply(state.tr.setSelection(TextSelection.create(state.doc, from, to)));
  for (const name of commands) expect(richCommand(name, state, tr => { state = state.apply(tr); })).toBe(true);
  state = state.apply(state.tr.addMark(from, to, richSchema.marks.link.create({ href })));
  const emitted = joinMarkdown(updateMarkdownBlock(blocks, index, state.doc));
  const reopened = splitMarkdown(emitted)[index].doc;
  expect(reopened.eq(state.doc)).toBe(true);
  expect(reopened.textContent).toBe(body);
  const target = reopened.lastChild.lastChild;
  expect(target.text).toBe('改。');
  expect(target.marks.find(mark => mark.type.name === 'link').attrs.href).toBe(href);
  const expected = prefix + body.slice(0, -2) + `[${delimiter}改。${delimiter}](${href})` + tail;
  expect(emitted).toBe(expected);
  expect([...new TextEncoder().encode(emitted)]).toEqual([...new TextEncoder().encode(expected)]);
});
it.each(emphasisLinks)('preserves existing %s across the start of a link followed by plain Chinese', (_, commands, delimiter) => {
  const body = '开头。已改。后文', href = 'https://example.invalid/q51-edited';
  let state = createRichState(richParser.parse(body));
  const from = 1 + body.indexOf('改。'), to = from + 2;
  state = state.apply(state.tr.setSelection(TextSelection.create(state.doc, from - 1, to)));
  for (const name of commands) expect(richCommand(name, state, tr => { state = state.apply(tr); })).toBe(true);
  state = state.apply(state.tr.addMark(from, to, richSchema.marks.link.create({ href })));
  const emitted = richSerializer.serialize(state.doc), reopened = richParser.parse(emitted);
  expect(reopened.eq(state.doc)).toBe(true);
  expect(reopened.textContent).toBe(body);
  expect(emitted).toBe(`开头。${delimiter}已${delimiter}[${delimiter}改。${delimiter}](${href})后文`);
});
it('retains escaped link attributes when reopening combined emphasis beside Chinese text', () => {
  let state = createRichState(richParser.parse('已改。后文'));
  state = state.apply(state.tr.setSelection(TextSelection.create(state.doc, 2, 4)));
  for (const name of ['bold', 'italic']) expect(richCommand(name, state, tr => { state = state.apply(tr); })).toBe(true);
  const attrs = { href: 'https://example.invalid/a(b)?x=1', title: '引号 "证据"' };
  state = state.apply(state.tr.addMark(2, 4, richSchema.marks.link.create(attrs)));
  const reopened = richParser.parse(richSerializer.serialize(state.doc));
  expect(reopened.eq(state.doc)).toBe(true);
  expect(reopened.textContent).toBe('已改。后文');
  const links = []; reopened.descendants(node => { for (const mark of node.marks) if (mark.type.name === 'link') links.push(mark.attrs); });
  expect(links).toEqual([attrs]);
});
it.each(['bullet-list', 'ordered-list'])('creates a %s without replacing the edited object', name => {
  let state = selected(); expect(richCommand(name, state, tr => { state = state.apply(tr); })).toBe(true);
  expect(state.doc.firstChild.type.name).toBe(name === 'bullet-list' ? 'bullet_list' : 'ordered_list');
  expect(state.doc.textContent).toBe('selected text');
});
it('edits table cells and adds and deletes both rows and columns', () => {
  let state = createRichState(richParser.parse('| a | b |\n| --- | --- |\n| c | d |'));
  state = state.apply(state.tr.setSelection(TextSelection.create(state.doc, 4)));
  state = state.apply(state.tr.insertText('甲', 4, 5));
  const execute = name => expect(richCommand(name, state, tr => { state = state.apply(tr); })).toBe(true);
  execute('row-add'); expect(state.doc.firstChild.childCount).toBe(3);
  execute('column-add'); expect(state.doc.firstChild.firstChild.childCount).toBe(3);
  execute('column-delete'); expect(state.doc.firstChild.firstChild.childCount).toBe(2);
  execute('row-delete'); expect(state.doc.firstChild.childCount).toBe(2);
  expect(richSerializer.serialize(state.doc)).toContain('|');
});
it('keeps edited line breaks inside table cells editable after reopening', () => {
  let state = createRichState(richParser.parse('| ab | c |\n| --- | --- |\n| d | e |'));
  state = state.apply(state.tr.insert(5, richSchema.nodes.hard_break.create()));
  const markdown = richSerializer.serialize(state.doc);
  expect(markdown).toContain('a<br>b');
  const reopened = richParser.parse(markdown);
  expect(reopened.textContent).toBe('abcde');
  let breaks = 0; reopened.descendants(node => { if (node.type.name === 'hard_break') breaks++; }); expect(breaks).toBe(1);
});
