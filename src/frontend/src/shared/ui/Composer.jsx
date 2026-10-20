import { useEffect, useRef, useState } from 'react';
import { Icon } from './Icon';
import { formatScopeTag, scopedText } from '../lib/scopeTag';
import './composer.css';
// 记住、问、干活合并成一个输入框：平时不带意图发送，由后端路由判断和拆分；
// 只有从别处带来的明确意图（例如"接着写"、按场景记住）才随这一次发送。
export function Composer({ intent, onIntent, onSend, projectTag, onRecord, recording = false, disabled = false, draft, onDraft, handoff, onHandoff, prefill, resetContinuation = 0, tools }) {
  const [text,setText] = useState(draft?.text || '');
  const [files,setFiles] = useState(draft?.files || []);
  const [selection,setSelection] = useState(draft?.selection ?? null);
  const [scopeTag,setScopeTag] = useState(draft?.scopeTag || null);
  const locked = useRef(draft?.locked || false);
  const input = useRef(null), applied = useRef(null), prefilled = useRef(null);
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
  const selected = onIntent ? intent : selection ?? intent;
  const continuationReset = useRef(resetContinuation);
  const currentDraft = useRef(null);
  currentDraft.current = { text, files, selection, locked: locked.current, scopeTag };
  function pick(next, manual = false) {
    if (manual) { locked.current = true; onDraft?.({ text, files, selection: next, locked: true, scopeTag }); }
    setSelection(next); onIntent?.(next);
  }
  useEffect(() => {
    if (!prefill || prefilled.current === prefill.id) return;
    prefilled.current = prefill.id; setScopeTag(null); setText(prefill.text); pick(prefill.intent, true); input.current?.focus();
  }, [prefill]);
  useEffect(() => {
    if (continuationReset.current === resetContinuation) return;
    continuationReset.current = resetContinuation; prefilled.current = null;
    if (selected === 'do') { setSelection(null); locked.current = false; onIntent?.(null); }
  }, [resetContinuation]);
  function changeText(next) {
    const nextTag = scopedText(next).tag;
    if (scopeTag && nextTag && nextTag !== scopedText(text).tag) setScopeTag(null);
    setText(next);
  }
  function addFiles(values) {
    if (disabled) return;
    const additions = Array.from(values || []);
    if (additions.length) setFiles(current => [...current, ...additions]);
  }
  function transferFiles(event) {
    const transfer = event.dataTransfer;
    if (!transfer?.files.length && !Array.from(transfer?.types || []).includes('Files')) return;
    event.preventDefault();
    if (event.type === 'dragover') transfer.dropEffect = disabled ? 'none' : 'copy';
    else addFiles(transfer.files);
  }
  async function send() {
    if (disabled || recording || sending.current || !onSend || (!text.trim() && !files.length)) return;
    sending.current = true;
    const submitted = currentDraft.current;
    const settle = accepted => {
      if (accepted !== true && !accepted?.acceptedFiles?.length) return;
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
  return <div className="ui-composer" onDragOver={transferFiles} onDrop={transferFiles}>
    <textarea ref={input} aria-label="输入" rows={2} placeholder="丢进来，或者问我" value={text} disabled={disabled} onChange={event => changeText(event.target.value)} onPaste={event => addFiles(event.clipboardData.files)} onCompositionStart={() => { composing.current = true; }} onCompositionEnd={() => { composing.current = false; }} onKeyDown={event => { if (event.key === 'Enter' && !event.shiftKey && !composing.current && !event.nativeEvent.isComposing && event.keyCode !== 229) { event.preventDefault(); send(); } }}/>
    {!!files.length && <div className="ui-composer-files">{files.map((file,index) => <span key={`${file.name}-${index}`}><span>{file.name}</span><button type="button" aria-label={`移除 ${file.name}`} disabled={disabled} onClick={() => setFiles(files.filter((_,i) => i !== index))}><Icon name="close"/></button></span>)}</div>}
    <div className="ui-composer-tools">
      <input type="file" multiple ref={fileInput} aria-label="文件" hidden disabled={disabled} onChange={event => { addFiles(event.target.files); event.target.value = ''; }}/>
      <button type="button" className="ui-composer-icon" aria-label="添加文件" title="文件" disabled={disabled} onClick={() => fileInput.current?.click()}><Icon name="plus"/></button>
      <button type="button" className="ui-composer-icon" aria-label={recording ? '停止录音' : '录音'} aria-pressed={recording} title={recording ? '停止录音' : '录音'} disabled={disabled || !onRecord} onClick={async () => { const file = await onRecord?.(); if (file) setFiles(current => [...current, file]); }}><Icon name="mic"/></button>
      {tag && <span className="ui-composer-tag" title={tag}>{tag}</span>}
      {typeof tools === 'function' ? tools({ intent: selected, files }) : tools}
      <button type="button" className="ui-composer-send" aria-label="发送" disabled={disabled || recording || !onSend || (!text.trim() && !files.length)} onClick={send}><Icon name="send"/></button>
    </div>
  </div>;
}
export default Composer;
