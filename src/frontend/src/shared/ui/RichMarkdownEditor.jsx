import { useEffect, useRef, useState } from 'react';
import { EditorState, Plugin, TextSelection } from 'prosemirror-state';
import { Decoration, DecorationSet, EditorView } from 'prosemirror-view';
import { baseKeymap, setBlockType, toggleMark, wrapIn } from 'prosemirror-commands';
import { keymap } from 'prosemirror-keymap';
import { history, undo, redo } from 'prosemirror-history';
import { addColumnAfter, addRowAfter, deleteColumn, deleteRow, goToNextCell, tableEditing } from 'prosemirror-tables';
import { containsTasks, joinMarkdown, richSchema, splitMarkdown, taskStructure, updateMarkdownBlock } from './richMarkdown';
import { MarkdownBody } from './MarkdownBody';
import { Icon } from './Icon';
import { commentCountElement, commentSectionRange } from './CommentSection';
import './RichMarkdownEditor.css';

export function richCommand(name, state, dispatch) {
  if (containsTasks(state.doc)) return false;
  if (['bold', 'italic', 'code'].includes(name)) return toggleMark(richSchema.marks[{ bold: 'strong', italic: 'em', code: 'code' }[name]])(state, dispatch);
  if (name.startsWith('heading-')) return setBlockType(richSchema.nodes.heading, { level: Number(name.slice(-1)) })(state, dispatch);
  if (name === 'paragraph') return setBlockType(richSchema.nodes.paragraph)(state, dispatch);
  if (name === 'bullet-list' || name === 'ordered-list') {
    const type = richSchema.nodes[name === 'bullet-list' ? 'bullet_list' : 'ordered_list'];
    for (let depth = state.selection.$from.depth; depth > 0; depth--) {
      const node = state.selection.$from.node(depth);
      if (/^(bullet|ordered)_list$/.test(node.type.name)) { dispatch?.(state.tr.setNodeMarkup(state.selection.$from.before(depth), type, { ...node.attrs, order: 1 })); return true; }
    }
    return wrapIn(type, { order: 1, tight: true })(state, dispatch);
  }
  const commands = { 'row-add': addRowAfter, 'row-delete': deleteRow, 'column-add': addColumnAfter, 'column-delete': deleteColumn };
  if (commands[name]) return commands[name](state, dispatch);
  if (name === 'table') {
    const cell = header => (header ? richSchema.nodes.table_header : richSchema.nodes.table_cell).createAndFill();
    const table = richSchema.nodes.table.create(null, [richSchema.nodes.table_row.create(null, [cell(true), cell(true)]), richSchema.nodes.table_row.create(null, [cell(false), cell(false)])]);
    dispatch?.(state.tr.replaceSelectionWith(table).scrollIntoView()); return true;
  }
  return false;
}
export function createRichState(doc, handlers = {}) {
  const tasks = containsTasks(doc), shape = tasks && taskStructure(doc);
  const originalMarks = new Set(['[]']); doc.descendants(node => { if (node.isText) originalMarks.add(JSON.stringify(node.marks.map(mark => mark.toJSON()))); });
  return EditorState.create({ doc, plugins: [
    new Plugin({ filterTransaction(tr) {
      if (!tasks || !tr.docChanged) return true;
      let invalid = false; tr.doc.descendants(node => { if (node.isText && !originalMarks.has(JSON.stringify(node.marks.map(mark => mark.toJSON())))) invalid = true; });
      return !invalid && taskStructure(tr.doc) === shape;
    } }),
    history(), keymap({ 'Mod-b': (state, dispatch) => richCommand('bold', state, dispatch), 'Mod-i': (state, dispatch) => richCommand('italic', state, dispatch),
      'Mod-k': () => { if (!tasks) handlers.link?.(); return true; }, 'Mod-s': () => { handlers.save?.(); return true; },
      'Mod-z': undo, 'Mod-y': redo, 'Shift-Mod-z': redo, Tab: goToNextCell(1), 'Shift-Tab': goToNextCell(-1),
    }), keymap(baseKeymap), tableEditing(),
  ] });
}
function Tool({ name, label, onClick, disabled, submit = false }) { return <button type={submit ? 'submit' : 'button'} aria-label={label} title={label} disabled={disabled} onClick={onClick}><Icon name={name} size={16}/></button>; }
function EditableBlock({ block, index, active, disabled, onActivate, onEdit, onSave, fact, onEvidence, commentCount }) {
  const host = useRef(null), viewRef = useRef(null), handlers = useRef({ onEdit, onSave }); handlers.current = { onEdit, onSave };
  const [selected, setSelected] = useState(false), [linking, setLinking] = useState(false), [href, setHref] = useState(''), [heading, setHeading] = useState(false);
  const [cellEdge, setCellEdge] = useState(null);
  const tasks = containsTasks(block.doc);
  useEffect(() => {
    const view = new EditorView(host.current, {
      state: createRichState(block.doc, { link: () => { setHref(''); setLinking(true); }, save: () => handlers.current.onSave() }), editable: () => false,
      attributes: { 'aria-label': `编辑块 ${index + 1}` },
      dispatchTransaction(tr) {
        const previous = view.state.doc, next = view.state.apply(tr); view.updateState(next); setSelected(!next.selection.empty);
        if (!next.doc.eq(previous)) handlers.current.onEdit(index, next.doc);
      },
      handleDOMEvents: { click: (_, event) => { if (event.target.closest('a')) event.preventDefault(); return false; } },
    }); viewRef.current = view;
    return () => { view.destroy(); viewRef.current = null; };
  }, [block.raw, index]);
  useEffect(() => {
    const view = viewRef.current; if (!view) return;
    view.setProps({ editable: () => active && !disabled, attributes: { 'aria-label': `编辑块 ${index + 1}`, role: active ? 'textbox' : 'group', tabindex: disabled ? '-1' : '0' } });
    if (active && !disabled) view.focus(); if (!active) { setLinking(false); setHeading(false); }
  }, [active, disabled, index]);
  useEffect(() => {
    const view = viewRef.current; if (!view) return;
    view.setProps({ decorations: state => {
      const heading = state.doc.firstChild;
      if (!commentCount || heading?.type.name !== 'heading' || heading.attrs.level !== 2 || heading.textContent !== '评论区') return DecorationSet.empty;
      return DecorationSet.create(state.doc, [Decoration.widget(heading.nodeSize - 1,
        () => commentCountElement(commentCount, view.dom.ownerDocument), { key: `comment-count:${commentCount}`, side: 1 })]);
    } });
  }, [commentCount, block.raw, index]);
  function command(name) { const view = viewRef.current; richCommand(name, view.state, view.dispatch); view.focus(); }
  function activate(event) {
    if (disabled) return;
    const view = viewRef.current, selection = view.dom.ownerDocument.getSelection();
    if (selection?.anchorNode && view.dom.contains(selection.anchorNode)) {
      const from = view.posAtDOM(selection.anchorNode, selection.anchorOffset), to = view.posAtDOM(selection.focusNode, selection.focusOffset);
      if (from >= 0 && to >= 0) view.dispatch(view.state.tr.setSelection(TextSelection.between(view.state.doc.resolve(from), view.state.doc.resolve(to))));
    }
    onActivate(index);
  }
  function hoverCell(event) {
    const cell = event.target.closest('td,th'); if (!cell || disabled) return;
    const root = event.currentTarget.getBoundingClientRect(), edge = cell.getBoundingClientRect();
    setCellEdge({ row: edge.top - root.top + edge.height / 2, column: Math.max(40, Math.min(root.width - 40, edge.left - root.left + edge.width / 2)), cell });
  }
  function tableCommand(name) {
    const view = viewRef.current;
    if (cellEdge?.cell?.isConnected) {
      const position = view.posAtDOM(cellEdge.cell, 0);
      view.dispatch(view.state.tr.setSelection(TextSelection.near(view.state.doc.resolve(position))));
    }
    onActivate(index); command(name);
  }
  function applyLink(event) {
    event.preventDefault(); const view = viewRef.current, value = href.trim();
    if (value && !/^(?:https?:\/\/|mailto:|tel:|\/|#|\.\/|\.\.\/)/i.test(value)) return;
    const { from, to } = view.state.selection; if (from === to) return;
    view.dispatch(value ? view.state.tr.addMark(from, to, richSchema.marks.link.create({ href: value })) : view.state.tr.removeMark(from, to, richSchema.marks.link));
    setLinking(false); view.focus();
  }
  return <div className={`ui-rich-block ${active ? 'is-editing' : ''} is-${block.kind}`} onClick={event => { if (event.target.closest('.ProseMirror')) activate(event); }} onMouseOver={hoverCell} onFocus={event => { if (event.target === viewRef.current?.dom && !disabled) onActivate(index); }}>
    {active && selected && !tasks && <div className="ui-rich-toolbar" role="toolbar" aria-label="文字格式" onMouseDown={event => event.preventDefault()}>
      {[['bold', '加粗'], ['italic', '斜体'], ['code', '行内代码']].map(([name, label]) => <Tool key={name} name={name} label={label} disabled={disabled} onClick={() => command(name)}/>)}
      <Tool name="link" label="链接" disabled={disabled} onClick={() => { setHref(''); setLinking(true); }}/><Tool name="heading" label="标题级别" disabled={disabled} onClick={() => setHeading(value => !value)}/>
      {[['bullet-list', '无序列表'], ['ordered-list', '有序列表'], ['table', '表格']].map(([name, label]) => <Tool key={name} name={name} label={label} disabled={disabled} onClick={() => command(name)}/>)}
      {heading && <div className="ui-rich-heading-options">{[1, 2, 3, 4, 5, 6].map(level => <Tool key={level} name={`heading-${level}`} label={`${level}级标题`} disabled={disabled} onClick={() => { command(`heading-${level}`); setHeading(false); }}/>) }<Tool name="paragraph" label="正文" disabled={disabled} onClick={() => command('paragraph')}/></div>}
    </div>}
    {active && linking && <form className="ui-rich-link" onSubmit={applyLink}><input aria-label="链接地址" value={href} onChange={event => setHref(event.target.value)} autoFocus disabled={disabled}/><Tool name="check" label="应用链接" disabled={disabled} submit/><Tool name="close" label="关闭链接" onClick={() => { setLinking(false); viewRef.current.focus(); }}/></form>}
    {block.kind === 'table' && !tasks && <><div className="ui-rich-table-controls ui-rich-row-controls" style={cellEdge ? { top: cellEdge.row } : undefined} role="toolbar" aria-label="表格行" onMouseDown={event => event.preventDefault()}>
      <Tool name="plus" label="增加行" disabled={disabled} onClick={() => tableCommand('row-add')}/><Tool name="minus" label="删除行" disabled={disabled} onClick={() => tableCommand('row-delete')}/>
    </div><div className="ui-rich-table-controls ui-rich-column-controls" style={cellEdge ? { left: cellEdge.column } : undefined} role="toolbar" aria-label="表格列" onMouseDown={event => event.preventDefault()}>
      <Tool name="plus" label="增加列" disabled={disabled} onClick={() => tableCommand('column-add')}/><Tool name="minus" label="删除列" disabled={disabled} onClick={() => tableCommand('column-delete')}/>
    </div></>}
    <div ref={host}/>
    {fact && onEvidence && <button className="ui-rich-evidence" type="button" aria-label={`定位原文：${fact.text}`} disabled={disabled} onClick={event => { event.stopPropagation(); onEvidence(fact); }}><Icon name="open" size={14}/></button>}
  </div>;
}
export function richHeadingSections(blocks) {
  const rows = [], stack = [];
  for (const [index, block] of blocks.entries()) {
    if (block.kind !== 'heading' || !block.doc) continue;
    const heading = (block.changed ?? block.raw).match(/^ {0,3}(#{1,6})[ \t]+(.+?)(?:[ \t]+#+)?[ \t]*(?:\r?\n|\r|$)/);
    if (!heading) continue;
    const level = heading[1].length;
    while (stack.length && stack.at(-1).level >= level) stack.pop();
    const path = [...(stack.at(-1)?.path ?? []), heading[2]], row = { path, index, level };
    rows.push(row); stack.push(row);
  }
  // 只在原分节器证明的标题块上匹配，重名完整路径不推测归属。
  return rows.map((row, index) => {
    const end = rows[index + 1]?.index ?? blocks.length;
    return { ...row, endIndex: end - 1, body: blocks.slice(row.index + 1, end).map(block => block.changed ?? block.raw).join(''),
      unique: rows.filter(other => JSON.stringify(other.path) === JSON.stringify(row.path)).length === 1 };
  });
}

export function RichMarkdownEditor({ value = '', onChange, onSave, disabled = false, label = '整理稿正文', evidence = [], onEvidence, documentId, documentRevision, documentMarkdown, commentSection, renderHeading }) {
  const [blocks, setBlocks] = useState(() => splitMarkdown(value)), [active, setActive] = useState(null), [mode, setMode] = useState('rendered'), [more, setMore] = useState(false);
  const blocksRef = useRef(blocks), emitted = useRef(value), callbacks = useRef({ onChange, onSave }); callbacks.current = { onChange, onSave }; blocksRef.current = blocks;
  useEffect(() => { if (value !== emitted.current) { emitted.current = value; const next = splitMarkdown(value); blocksRef.current = next; setBlocks(next); setActive(null); } }, [value]);
  function edit(index, doc) { const next = updateMarkdownBlock(blocksRef.current, index, doc); blocksRef.current = next; setBlocks(next); emitted.current = joinMarkdown(next); callbacks.current.onChange?.(emitted.current); }
  function save() { if (!disabled) callbacks.current.onSave?.(mode === 'source' ? value : joinMarkdown(blocksRef.current)); }
  function switchMode(next) { if (next === 'rendered') { const parsed = splitMarkdown(value); blocksRef.current = parsed; setBlocks(parsed); setActive(null); } setMode(next); setMore(false); }
  const visibleText = blocks.map(block => (block.currentDoc || block.doc)?.textContent.trim());
  const section = value === documentMarkdown ? commentSectionRange({ markdown: value, documentId, documentRevision, commentSection }) : null;
  const decorations = renderHeading ? richHeadingSections(blocks).filter(row => row.unique).map(row => ({ row, value: renderHeading({ path: row.path, block: blocks[row.index] }) })) : [];
  return <div className="ui-rich-editor" onKeyDown={event => { if (!event.defaultPrevented && (event.ctrlKey || event.metaKey) && event.key.toLowerCase() === 's') { event.preventDefault(); event.stopPropagation(); save(); } }}>
    <div className="ui-rich-actions"><Tool name="more" label="更多编辑" disabled={disabled} onClick={() => setMore(value => !value)}/>{more && <button type="button" disabled={disabled} onClick={() => switchMode(mode === 'source' ? 'rendered' : 'source')}>{mode === 'source' ? '整理稿' : '源码'}</button>}<Tool name="save" label="保存" disabled={disabled} onClick={save}/></div>
    {mode === 'source' ? <textarea aria-label={label} value={value} disabled={disabled} onChange={event => { emitted.current = event.target.value; callbacks.current.onChange?.(event.target.value); }}/>
      : <div className="ui-markdown-body ui-rich-rendered">{blocks.map((block, index) => {
        const matches = visibleText.filter(text => text === visibleText[index]).length === 1 ? evidence.filter(fact => fact.text === visibleText[index]) : [];
        const content = block.separator ? null : block.doc ? <EditableBlock key={`${index}:${block.raw}`} block={block} index={index} active={active === index} disabled={disabled} onActivate={setActive} onEdit={edit} onSave={save} fact={matches.length === 1 ? matches[0] : null} onEvidence={onEvidence} commentCount={section && block.start === section.start && block.end === section.end ? section.count : null}/> : <MarkdownBody key={index}>{block.raw}</MarkdownBody>;
        const before = decorations.find(entry => entry.row.index === index)?.value?.before;
        const after = decorations.find(entry => entry.row.endIndex === index)?.value?.after;
        return before || after ? <div key={`${index}:${block.raw}`}>{before ? <div style={{ display: 'flex', alignItems: 'baseline' }}>{before}<div style={{ flex: 1, minWidth: 0 }}>{content}</div></div> : content}{after}</div> : content;
      })}</div>}
  </div>;
}
export default RichMarkdownEditor;
