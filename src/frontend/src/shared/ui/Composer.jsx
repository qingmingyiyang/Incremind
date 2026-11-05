import { useEffect, useRef, useState } from 'react';
import { Icon } from './Icon';
import { formatScopeTag, scopedText } from '../lib/scopeTag';
import './composer.css';
// 记住、问、干活合并成一个输入框：平时不带意图发送，由后端路由判断和拆分；
// 只有从别处带来的明确意图（例如"接着写"、按场景记住）才随这一次发送。
export function Composer({ intent, fixedIntent, allowedIntents, allowFiles = true, onIntent, onSend, projectTag, projects = [], onExecutor, executorIntent, onAutomatic, onRemind, onRecord, recording = false, disabled = false, sendDisabled = false, draft, onDraft, handoff, onHandoff, prefill, onPrefill, resetContinuation = 0, tools, interventionDraft }) {
  const [text,setText] = useState(draft?.text || '');
  const [files,setFiles] = useState(draft?.files || []);
  const [selection,setSelection] = useState(fixedIntent ?? draft?.selection ?? null);
  const [scopeTag,setScopeTag] = useState(draft?.scopeTag || null);
  const locked = useRef(draft?.locked || false);
  const input = useRef(null), applied = useRef(null), prefilled = useRef(null);
  const [menu, setMenu] = useState(null);
  const [proposal, setProposal] = useState(null);
  const appliedIntervention = useRef(null);
  const menuElement = useRef(null);
  useEffect(() => { onDraft?.({ text, files, selection, locked: locked.current, scopeTag }); }, [text, files, selection, scopeTag, onDraft]);
  useEffect(() => {
    if (!handoff || applied.current === handoff.id || disabled) return;
    applied.current = handoff.id; locked.current = true;
    setSelection('remember'); onIntent?.('remember');
    const name = String(projectTag || ''), scene = handoff.scene ? '/' + handoff.scene : '';
    setScopeTag({ label: `#${name}${scene}`, tag: formatScopeTag(handoff.projectId || name, handoff.scene || null) });
    input.current?.focus(); onHandoff?.(handoff.id);
  }, [handoff, disabled, projectTag, onIntent, onHandoff]);
  const composing = useRef(false);
  const fileInput = useRef(null);
  const sending = useRef(false);
  const requested = fixedIntent || (onIntent ? intent : selection ?? intent);
  const selected = allowedIntents ? allowedIntents.includes(requested) ? requested : allowedIntents[0] : requested;
  const continuationReset = useRef(resetContinuation);
  const currentDraft = useRef(null);
  currentDraft.current = { text, files, selection, locked: locked.current, scopeTag };
  function pick(next, manual = false) {
    if (fixedIntent || allowedIntents && !allowedIntents.includes(next)) return;
    if (manual) { locked.current = true; onDraft?.({ text, files, selection: next, locked: true, scopeTag }); }
    setSelection(next); onIntent?.(next);
  }
  useEffect(() => {
    if (!interventionDraft || disabled || appliedIntervention.current === interventionDraft) return;
    appliedIntervention.current = interventionDraft;
    // 转入新轮时保留已有正文与附件，由用户核对后发送。
    const current = currentDraft.current;
    const next = { ...current, text: current.text ? `${current.text}\n${interventionDraft.text}` : interventionDraft.text,
      selection: null, locked: false };
    currentDraft.current = next; onDraft?.(next); setText(next.text);
    locked.current = false; pick(null); onAutomatic?.();
    interventionDraft.accept(); input.current?.focus();
  }, [interventionDraft, disabled]);
  useEffect(() => {
    if (!prefill || prefilled.current === prefill.id) return;
    prefilled.current = prefill.id;
    if (prefill.confirmReplace && (text.trim() || files.length)) { setProposal(prefill); return; }
    applyPrefill(prefill);
  }, [prefill]);
  function applyPrefill(value) {
    setScopeTag(null); setText(value.text); pick(value.intent, true); input.current?.focus();
    onPrefill?.(value.id);
  }
  useEffect(() => {
    if (continuationReset.current === resetContinuation) return;
    continuationReset.current = resetContinuation; prefilled.current = null;
    if (selected === 'do') { setSelection(null); locked.current = false; onIntent?.(null); }
  }, [resetContinuation]);
  function changeText(next) {
    const nextTag = scopedText(next).tag;
    if (scopeTag && nextTag && nextTag !== scopedText(text).tag) setScopeTag(null);
    setText(next);
    if (composing.current) return;
    if (!fixedIntent && (!allowedIntents || projects.length > 0) && /(?:^|\s)#$/.test(next)) setMenu('scope');
    else if (!fixedIntent && /(?:^|\s)\/$/.test(next)) setMenu('action');
    else if (onExecutor && !files.length && (!selected || selected === 'do') && /(?:^|\s)@$/.test(next)) { setText(next.slice(0, -1)); setMenu(null); onExecutor(); }
    else setMenu(null);
  }
  function stripShortcut() { return text.replace(/(^|\s)[#/]$/, '$1'); }
  function chooseScope(project, scene = null) {
    setScopeTag({ label: `#${project.name}${scene ? '/' + scene : ''}`, tag: formatScopeTag(project.id, scene) });
    setText(stripShortcut()); setMenu(null); input.current?.focus();
  }
  function chooseAction(next) {
    setText(stripShortcut()); setMenu(null); pick(next, true); input.current?.focus();
    if (!next) onAutomatic?.();
  }
  function menuKey(event) {
    if (event.key === 'Escape') { event.preventDefault(); event.stopPropagation(); setMenu(null); input.current?.focus(); }
    if (['ArrowDown', 'ArrowUp', 'Home', 'End'].includes(event.key)) {
      event.preventDefault();
      const buttons = [...menuElement.current.querySelectorAll('button:not(:disabled)')];
      const index = buttons.indexOf(document.activeElement);
      buttons[event.key === 'Home' ? 0 : event.key === 'End' ? buttons.length - 1
        : (index + (event.key === 'ArrowDown' ? 1 : -1) + buttons.length) % buttons.length]?.focus();
    }
  }
  function addFiles(values) {
    if (disabled || !allowFiles) return;
    const additions = Array.from(values || []);
    if (additions.length) setFiles(current => [...current, ...additions]);
  }
  function transferFiles(event) {
    const transfer = event.dataTransfer;
    if (!transfer?.files.length && !Array.from(transfer?.types || []).includes('Files')) return;
    event.preventDefault();
    if (event.type === 'dragover') transfer.dropEffect = disabled || !allowFiles ? 'none' : 'copy';
    else addFiles(transfer.files);
  }
  async function send() {
    if (disabled || sendDisabled || recording || sending.current || !onSend || (!text.trim() && !files.length)) return;
    sending.current = true;
    const submitted = currentDraft.current;
    let completed = false;
    const settle = accepted => {
      if (accepted !== true && !accepted?.acceptedFiles?.length) return;
      if (accepted === true && completed) return;
      if (accepted === true) completed = true;
      const current = currentDraft.current;
      const acceptedFiles = [...(accepted === true ? submitted.files : accepted.acceptedFiles)];
      const remaining = current.files.filter(file => {
        const index = acceptedFiles.indexOf(file);
        if (index < 0) return true;
        acceptedFiles.splice(index, 1); return false;
      });
      const complete = accepted === true;
      const sameText = current.text === submitted.text;
      const sameSelection = current.selection === submitted.selection;
      const next = { ...current, files: remaining,
        text: complete && sameText ? '' : current.text,
        selection: complete && sameSelection ? null : current.selection,
        locked: complete && sameSelection ? false : current.locked,
        scopeTag: complete && sameText && current.scopeTag === submitted.scopeTag ? null : current.scopeTag };
      // 按接受的附件逐项更新，跨项目卸载也同步保留来源草稿中的新编辑。
      currentDraft.current = next; onDraft?.(next);
      setText(next.text); setFiles(next.files); setSelection(next.selection); setScopeTag(next.scopeTag); locked.current = next.locked;
    };
    try {
      const accepted = await onSend({ text: scopeTag ? `${scopeTag.tag}\n${scopedText(text).text}` : text, files, intent: selected, onAccepted: settle });
      settle(accepted);
    } finally { sending.current = false; }
  }
  // 只显示输入里写的或从别处带入的范围；当前项目已经显示在顶部的项目切换里。
  const tag = scopeTag?.label || scopedText(text).label;
  const actionLabel = { remember: '记住', ask: '问', do: '干活', inspiration: '灵感' };
  const clean = scopedText(text).text;
  const guessed = files.length || /^https?:\/\//i.test(clean) || /^灵感/.test(clean) ? 'remember'
    : /^(帮我|请帮|写|整理一份|做一份)/.test(clean) ? 'do'
    : /[？?]$/.test(clean) || /^(什么|怎么|为什么|哪|上次)/.test(clean) ? 'ask' : null;
  const forced = selected || (!files.length ? executorIntent : null);
  return <div className="ui-composer" onDragOver={transferFiles} onDrop={transferFiles} onBlur={event => { if (!event.currentTarget.contains(event.relatedTarget)) setMenu(null); }}>
    {menu && <div ref={menuElement} className="ui-composer-menu" role="menu" aria-label={menu === 'scope' ? '项目与场景' : '动作'} onKeyDown={menuKey}>
      <div className="ui-composer-menu-title">{menu === 'scope' ? '项目 · 场景' : '动作'}</div>
      {menu === 'scope' ? projects.flatMap(project => [<button key={project.id} role="menuitem" type="button" disabled={disabled} onClick={() => chooseScope(project)}><span>#</span>{project.name}</button>,
        ...(project.scenes || []).map(scene => <button key={`${project.id}:${scene}`} role="menuitem" type="button" disabled={disabled} onClick={() => chooseScope(project, scene)}><span>#</span>{project.name}/{scene}</button>)])
        : <>{Object.entries(actionLabel).filter(([key]) => !allowedIntents || allowedIntents.includes(key)).map(([key, label]) => <button key={key} type="button" role="menuitem" disabled={disabled} onClick={() => chooseAction(key)}><Icon name={key} size={16}/>{label}<span className="ui-composer-menu-hint">{key === 'remember' ? '存进资料库' : key === 'ask' ? '从记忆里找' : key === 'do' ? '写成一份成果' : allowedIntents ? '存到共享项目' : '先放收件箱'}</span></button>)}
          {onRemind && <button type="button" role="menuitem" disabled={disabled} onClick={() => { setText(stripShortcut()); setMenu(null); onRemind(); }}><Icon name="calendar" size={16}/>提醒<span className="ui-composer-menu-hint">安排时间</span></button>}
          {!allowedIntents && <button type="button" role="menuitem" disabled={disabled} onClick={() => chooseAction(null)}><Icon name="workspace" size={16}/>自动判断</button>}</>}
    </div>}
    {proposal && <div className="ui-composer-proposal" role="group" aria-label="试问输入选择"><span>输入中有草稿</span><button type="button" disabled={disabled} onClick={() => { onPrefill?.(proposal.id); setProposal(null); input.current?.focus(); }}>保留草稿</button><button type="button" disabled={disabled} onClick={() => { applyPrefill(proposal); setProposal(null); }}>使用试问</button></div>}
    <textarea ref={input} aria-label="输入" rows={2} placeholder="丢进来，或者问我" value={text} disabled={disabled} onChange={event => changeText(event.target.value)} onPaste={event => addFiles(event.clipboardData.files)} onCompositionStart={() => { composing.current = true; }} onCompositionEnd={() => { composing.current = false; }} onKeyDown={event => {
      if (menu && event.key === 'Escape') { event.preventDefault(); setMenu(null); return; }
      if (menu && event.key === 'ArrowDown') { event.preventDefault(); menuElement.current?.querySelector('button')?.focus(); return; }
      if (event.key === 'Enter' && !event.shiftKey && !composing.current && !event.nativeEvent.isComposing && event.keyCode !== 229) { event.preventDefault(); setMenu(null); send(); }
    }}/>
    {!!files.length && <div className="ui-composer-files">{files.map((file,index) => <span key={`${file.name}-${index}`}><span>{file.name}</span><button type="button" aria-label={`移除 ${file.name}`} disabled={disabled} onClick={() => setFiles(files.filter((_,i) => i !== index))}><Icon name="close"/></button></span>)}</div>}
    <div className="ui-composer-tools">
      <input type="file" multiple ref={fileInput} aria-label="文件" hidden disabled={disabled || !allowFiles} onChange={event => { addFiles(event.target.files); event.target.value = ''; }}/>
      <button type="button" className="ui-composer-icon" aria-label="添加文件" title="文件" disabled={disabled || !allowFiles} onClick={() => fileInput.current?.click()}><Icon name="plus"/></button>
      <button type="button" className="ui-composer-icon" aria-label={recording ? '停止录音' : '录音'} aria-pressed={recording} title={recording ? '停止录音' : '录音'} disabled={disabled || !allowFiles || !onRecord} onClick={async () => { const file = await onRecord?.(); if (file) setFiles(current => [...current, file]); }}><Icon name="mic"/></button>
      {projects.length > 0 ? <button type="button" className={`ui-composer-tag ${tag ? 'is-set' : ''}`} aria-label="选择项目或场景" aria-haspopup="menu" aria-expanded={menu === 'scope'} title={tag || '选择项目或场景，也可以输入 #'} disabled={disabled} onClick={() => setMenu(menu === 'scope' ? null : 'scope')}>{tag || '#项目'}</button>
        : tag && <span className="ui-composer-tag" title={tag}>{tag}</span>}
      <button type="button" className="ui-composer-shortcut" aria-label="选择动作" aria-haspopup="menu" aria-expanded={menu === 'action'} title="选择动作，也可以输入 /" disabled={disabled || Boolean(fixedIntent)} onClick={() => setMenu(menu === 'action' ? null : 'action')}>/</button>
      {typeof tools === 'function' ? tools({ intent: selected, files }) : tools}
      {(forced || clean || files.length > 0) && <span className="ui-composer-route" title={forced ? '发送时使用所选动作' : '根据输入预览，实际处理由发送后的路由决定'}><Icon name={forced || guessed || 'workspace'} size={12}/><span>{forced ? actionLabel[forced] : guessed ? `会判成${actionLabel[guessed]}` : '自动判断'}</span>{forced && !fixedIntent && !allowedIntents && <button type="button" aria-label="改回自动判断" disabled={disabled} onClick={() => { locked.current = false; pick(null); onAutomatic?.(); }}><Icon name="close" size={12}/></button>}</span>}
      <button type="button" className="ui-composer-send" aria-label="发送" disabled={disabled || sendDisabled || recording || !onSend || (!text.trim() && !files.length)} onClick={send}><Icon name="send"/></button>
    </div>
  </div>;
}
export default Composer;
