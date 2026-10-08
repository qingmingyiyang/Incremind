import { Schema } from 'prosemirror-model';
import { MarkdownParser, MarkdownSerializer, defaultMarkdownParser, defaultMarkdownSerializer, schema as markdownSchema } from 'prosemirror-markdown';
import { tableNodes } from 'prosemirror-tables';

const itemSpec = markdownSchema.spec.nodes.get('list_item');
const imageSpec = markdownSchema.spec.nodes.get('image');
const safeHref = value => /^(?:https?:|mailto:|tel:|\/|#|\.\/|\.\.\/)/i.test(value || '') ? value : '';
export const richSchema = new Schema({
  nodes: markdownSchema.spec.nodes
    .update('list_item', { ...itemSpec, attrs: { task: { default: null }, textOnly: { default: false } }, toDOM(node) {
      return node.attrs.task === null ? ['li', 0] : ['li', { 'data-task-state': node.attrs.task },
        ['span', { contenteditable: 'false', 'aria-label': '待办', class: 'ui-rich-task-marker' }, node.attrs.task === ' ' ? '○' : '●'], ['div', 0]];
    } })
    // Embedded images remain inert, including while the block is being edited.
    .update('image', { ...imageSpec, toDOM: node => ['span', { 'data-image-alt': true }, node.attrs.alt || ''] })
    .append(tableNodes({ tableGroup: 'block', cellContent: 'paragraph+' })),
  marks: markdownSchema.spec.marks.addBefore('em', 'link', { ...markdownSchema.spec.marks.get('link'),
    toDOM: mark => ['a', { href: safeHref(mark.attrs.href), title: mark.attrs.title }, 0] }),
});

const tokenizer = new defaultMarkdownParser.tokenizer.constructor('commonmark', { html: true });
tokenizer.enable(['table', 'strikethrough']);
tokenizer.core.ruler.after('inline', 'rich-blocks', state => {
  const items = [];
  const result = [];
  for (const token of state.tokens) {
    if (token.type === 'list_item_open') items.push(token);
    if (token.type === 'list_item_close') items.pop();
    if (token.type === 'inline' && items.length) {
      const first = token.children?.[0], marker = first?.type === 'text' && first.content.match(/^\[([ xX])\]\s+/);
      if (marker && !items.at(-1).meta?.hasText) {
        items.at(-1).meta = { task: marker[1], hasText: true };
        first.content = first.content.slice(marker[0].length);
      } else items.at(-1).meta = { ...items.at(-1).meta, hasText: true };
    }
    if (token.type === 'th_close' || token.type === 'td_close') result.push(new state.Token('paragraph_close', 'p', -1));
    result.push(token);
    if (token.type === 'th_open' || token.type === 'td_open') result.push(new state.Token('paragraph_open', 'p', 1));
  }
  state.tokens = result;
});
export const richParser = new MarkdownParser(richSchema, tokenizer, {
  ...defaultMarkdownParser.tokens,
  list_item: { block: 'list_item', getAttrs: token => ({ task: token.meta?.task ?? null }) },
  table: { block: 'table' }, tr: { block: 'table_row' }, th: { block: 'table_header' }, td: { block: 'table_cell' },
  thead: { ignore: true }, tbody: { ignore: true },
  html_inline: { node: 'hard_break', getAttrs: token => { if (!/^<br\s*\/?\s*>$/i.test(token.content)) throw new Error('unsupported inline HTML'); return null; } },
});
export const richSerializer = new MarkdownSerializer({
  ...defaultMarkdownSerializer.nodes,
  list_item(state, node) {
    if (node.attrs.task !== null) state.write(`[${node.attrs.task}] `);
    state.renderContent(node);
  },
  table(state, node) {
    const rows = [];
    node.forEach(row => {
      const cells = [];
      row.forEach(cell => {
        const text = richSerializer.serialize(richSchema.nodes.doc.create(null, cell.content));
        cells.push(text.replace(/\|/g, '\\|').replace(/\\\n/g, '<br>').replace(/\n/g, '<br>'));
      });
      rows.push(`| ${cells.join(' | ')} |`);
    });
    if (rows.length) rows.splice(1, 0, `| ${Array(node.firstChild.childCount).fill('---').join(' | ')} |`);
    state.write(rows.join('\n'));
    state.closeBlock(node);
  },
}, { ...defaultMarkdownSerializer.marks, link: { ...defaultMarkdownSerializer.marks.link, mixable: false } });

// Source offsets always address the original JS string, never tokenizer-normalized text.
export function splitMarkdown(markdown) {
  const starts = [0];
  for (let i = 0; i < markdown.length; i++) {
    if (markdown[i] === '\r') {
      if (markdown[i + 1] === '\n') i++;
      starts.push(i + 1);
    } else if (markdown[i] === '\n') starts.push(i + 1);
  }
  starts.push(markdown.length);
  const env = {}, tokens = tokenizer.parse(markdown, env), ranges = []; let todoSection = false;
  for (const [tokenIndex, token] of tokens.entries()) {
    if (token.type === 'heading_open' && token.level === 0 && Number(token.tag.slice(1)) <= 2) todoSection = token.tag === 'h2' && token.markup === '##' && tokens[tokenIndex + 1]?.content === '待办';
    if (!token.map || token.nesting === -1) continue;
    const item = token.type === 'list_item_open' && token.level === 1;
    if (!item && (token.level !== 0 || /^(bullet_list|ordered_list)_open$/.test(token.type))) continue;
    let start = starts[token.map[0]], end = starts[token.map[1]] ?? markdown.length;
    const original = markdown.slice(start, end);
    const eol = original.match(/\r\n|\r|\n/)?.[0] || markdown.slice(0, start).match(/(\r\n|\r|\n)[^\r\n]*$/)?.[1] || '\n';
    // Retain trailing CRLF, lone CR, and LF delimiters in the immutable separator.
    const suffix = original.match(/(?:(?:\r\n|\r|\n)[ \t]*)+$/)?.[0];
    if (suffix) end -= suffix.length;
    if (end > start) ranges.push({ start, end, eol, kind: item ? 'list_item' : token.type.replace(/_open$/, ''), textOnly: item && todoSection });
  }
  const blocks = []; let cursor = 0;
  for (const range of ranges) {
    if (range.start < cursor) continue;
    if (range.start > cursor) blocks.push({ raw: markdown.slice(cursor, range.start), separator: true });
    const raw = markdown.slice(range.start, range.end);
    let doc = null;
    try {
      doc = richParser.parse(raw, env);
      if (range.textOnly) {
        const protect = node => ({ ...node, ...(node.type === 'list_item' ? { attrs: { ...node.attrs, textOnly: true } } : {}), ...(node.content ? { content: node.content.map(protect) } : {}) });
        doc = richSchema.nodeFromJSON(protect(doc.toJSON()));
      }
      doc.check();
    } catch { /* Unsupported syntax remains byte-preserved and source-editable. */ }
    blocks.push({ ...range, raw, doc });
    cursor = range.end;
  }
  if (cursor < markdown.length) blocks.push({ raw: markdown.slice(cursor), separator: true });
  if (!blocks.length) blocks.push({ raw: markdown, doc: richParser.parse(''), eol: '\n', kind: 'paragraph' });
  return blocks;
}
export function joinMarkdown(blocks) { return blocks.map(block => block.changed ?? block.raw).join(''); }
export function updateMarkdownBlock(blocks, index, doc) {
  doc.check();
  const original = blocks[index];
  const changed = doc.eq(original.doc) ? undefined : richSerializer.serialize(doc).replace(/\n/g, original.eol);
  return blocks.map((block, i) => i === index ? { ...block, changed, currentDoc: doc } : block);
}
export function containsTasks(doc) { let found = false; doc.descendants(node => { if (node.attrs.task != null || node.attrs.textOnly) found = true; }); return found; }
export function taskStructure(doc) {
  const structure = node => ({ type: node.type.name, attrs: node.attrs, children: node.content.content.filter(child => !child.isText).map(structure) });
  return JSON.stringify(structure(doc));
}
