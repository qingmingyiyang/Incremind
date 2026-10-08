import { useEffect, useRef, useState } from 'react';
import { Icon } from './Icon';
import './composer.css';
function scopedText(text) {
  const tag = '#([^\\s/#]+)(?:/([^\\s/#]+))?';
  const matches = [new RegExp('^[ \\t]*' + tag + '(?=[ \\t]|\\r?$)[ \\t]*', 'm').exec(text), new RegExp('[ \\t]+' + tag + '[ \\t]*(?=\\r?$)', 'm').exec(text)].filter(Boolean).sort((a,b) => a.index-b.index);
  if (!matches.length) return { text: text.trim(), tag: null };
  const match = matches[0];
  return { text: (text.slice(0,match.index)+text.slice(match.index+match[0].length)).trim(), tag: `#${match[1]}${match[2] ? '/' + match[2] : ''}` };
}
// 记住、问、干活合并成一个输入框：平时不带意图发送，由后端路由判断和拆分；
// 只有从别处带来的明确意图（例如"接着写"、按场景记住）才随这一次发送。
export function Composer({ intent, onIntent, onSend, projectTag, onRecord, recording = false, disabled = false, draft, onDraft, handoff, onHandoff, prefill }) {
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
    const name = String(projectTag || '').replace(/^#/, ''), scene = handoff.scene ? '/' + handoff.scene : '';
    setScopeTag({ label: `#${name}${scene}`, tag: `#${handoff.projectId || name}${scene}` });
    input.current?.focus(); onHandoff?.(handoff.id);
  }, [handoff, disabled, projectTag, onIntent, onHandoff]);
  const composing = useRef(false);
  const fileInput = useRef(null);
  const sending = useRef(false);
  const selected = onIntent ? intent : selection ?? intent;
  function pick(next, manual = false) {
    if (manual) { locked.current = true; onDraft?.({ text, files, selection: next, locked: true, scopeTag }); }
    setSelection(next); onIntent?.(next);
  }
  useEffect(() => {
    if (!prefill || prefilled.current === prefill.id) return;
    prefilled.current = prefill.id; setScopeTag(null); setText(prefill.text); pick(prefill.intent, true); input.current?.focus();
  }, [prefill]);
  function changeText(next) {
    const nextTag = scopedText(next).tag;
    if (scopeTag && nextTag && nextTag !== scopedText(text).tag) setScopeTag(null);
    setText(next);
  }
  async function send() {
    if (disabled || recording || sending.current || !onSend || (!text.trim() && !files.length)) return;
    sending.current = true;
    try {
      const accepted = await onSend({ text: scopeTag ? `${scopeTag.tag} ${scopedText(text).text}` : text, files, intent: selected });
      if (accepted === false) return;
      // 已接受的跨项目发送会卸载输入框，通过原回调同步清除来源项目的内存草稿。
      if (accepted === true) onDraft?.({ text: '', files: [], selection: null, locked: false, scopeTag: null });
      setText(''); setFiles([]); setSelection(null); setScopeTag(null); locked.current = false;
    } finally { sending.current = false; }
  }
  // 只显示输入里写的或从别处带入的范围；当前项目已经显示在顶部的项目切换里。
  const tag = scopeTag?.label || scopedText(text).tag;
  return <div className="ui-composer">
    <textarea ref={input} aria-label="输入" rows={2} placeholder="丢进来，或者问我" value={text} disabled={disabled} onChange={event => changeText(event.target.value)} onCompositionStart={() => { composing.current = true; }} onCompositionEnd={() => { composing.current = false; }} onKeyDown={event => { if (event.key === 'Enter' && !event.shiftKey && !composing.current && !event.nativeEvent.isComposing && event.keyCode !== 229) { event.preventDefault(); send(); } }}/>
    {!!files.length && <div className="ui-composer-files">{files.map((file,index) => <span key={`${file.name}-${index}`}><span>{file.name}</span><button type="button" aria-label={`移除 ${file.name}`} disabled={disabled} onClick={() => setFiles(files.filter((_,i) => i !== index))}><Icon name="close"/></button></span>)}</div>}
    <div className="ui-composer-tools">
      <input type="file" multiple ref={fileInput} aria-label="文件" hidden onChange={event => { setFiles([...files,...Array.from(event.target.files || [])]); event.target.value = ''; }}/>
      <button type="button" className="ui-composer-icon" aria-label="添加文件" title="文件" disabled={disabled} onClick={() => fileInput.current?.click()}><Icon name="plus"/></button>
      <button type="button" className="ui-composer-icon" aria-label={recording ? '停止录音' : '录音'} aria-pressed={recording} title={recording ? '停止录音' : '录音'} disabled={disabled || !onRecord} onClick={async () => { const file = await onRecord?.(); if (file) setFiles(current => [...current, file]); }}><Icon name="mic"/></button>
      {tag && <span className="ui-composer-tag" title={tag}>{tag}</span>}
      <button type="button" className="ui-composer-send" aria-label="发送" disabled={disabled || recording || !onSend || (!text.trim() && !files.length)} onClick={send}><Icon name="send"/></button>
    </div>
  </div>;
}
export default Composer;
