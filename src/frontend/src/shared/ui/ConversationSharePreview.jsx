import { useEffect, useMemo, useRef, useState } from 'react';
import { useRequestScope } from '../lib/useRequestScope';
import { conversationSnapshotMarkdown, removeConversationCitation } from '../lib/conversationSnapshot';
import { createConversationImagePages } from '../lib/conversationImage';
import { FocusPanel } from './FocusPanel';
import { MarkdownBody } from './MarkdownBody';
import { Icon } from './Icon';
import { StatusDot } from './StatusDot';
import './conversationShare.css';

export function ConversationSharePreview({ snapshot, scope, loading, error: readError, onRetry, onClose }) {
  const session = useMemo(() => ({ scope, snapshot }), [scope, snapshot]), gate = useRequestScope(session);
  const [selection, setSelection] = useState({ session, snapshot });
  const [exportState, setExportState] = useState({ session, busy: '', error: '', copied: false, images: [] });
  const urls = useRef([]), timer = useRef(null);
  const current = selection.session === session ? selection.snapshot : snapshot;
  const state = exportState.session === session ? exportState : { busy: '', error: '', copied: false, images: [] };
  function releaseImages() { urls.current.forEach(url => URL.revokeObjectURL(url)); urls.current = []; }
  useEffect(() => {
    setSelection({ session, snapshot }); setExportState({ session, busy: '', error: '', copied: false, images: [] });
    return () => { releaseImages(); clearTimeout(timer.current); };
  }, [session]);
  function update(patch) { setExportState(previous => ({ ...(previous.session === session ? previous : { images: [], copied: false }), session, ...patch })); }
  async function copy() {
    if (!current || state.busy) return;
    const request = gate.issue('export', gate.scope); if (!request) return;
    update({ busy: 'copy', error: '', copied: false });
    try {
      await navigator.clipboard.writeText(conversationSnapshotMarkdown(current));
      if (gate.isCurrent(request)) {
        update({ copied: true }); clearTimeout(timer.current);
        timer.current = setTimeout(() => { if (gate.isCurrent(request)) update({ copied: false }); }, 1500);
      }
    } catch { if (gate.isCurrent(request)) update({ error: 'copy' }); }
    finally { if (gate.isCurrent(request)) update({ busy: '' }); }
  }
  async function image() {
    if (!current || state.busy) return;
    const request = gate.issue('export', gate.scope); if (!request) return;
    releaseImages(); update({ busy: 'image', error: '', images: [] });
    const created = [];
    try {
      const pages = await createConversationImagePages(current);
      if (!gate.isCurrent(request)) return;
      const images = pages.map(page => { const url = URL.createObjectURL(page.blob); created.push(url); return { url, filename: page.filename }; });
      urls.current = created; update({ images });
      // 首张直接保存，分页保留全部明确的下载入口，避免浏览器拦截连续下载。
      const link = document.createElement('a'); link.href = images[0].url; link.download = images[0].filename; link.click();
    } catch { created.forEach(url => URL.revokeObjectURL(url)); if (gate.isCurrent(request)) update({ error: 'image', images: [] }); }
    finally { if (gate.isCurrent(request)) update({ busy: '' }); }
  }
  function remove(index) {
    if (state.busy) return;
    gate.invalidate('export'); releaseImages(); clearTimeout(timer.current);
    setSelection({ session, snapshot: removeConversationCitation(current, index) }); update({ images: [], copied: false, error: '' });
  }
  function close() {
    gate.invalidate('export'); releaseImages(); clearTimeout(timer.current);
    update({ busy: '', images: [], copied: false, error: '' }); onClose?.();
  }
  return <FocusPanel title="分享" onClose={close} className="ui-conversation-share">
    {loading ? <StatusDot state="processing"/> : readError || !current ? <p role="alert">读取未完成 · <button type="button" onClick={onRetry}>重试</button></p> : <>
      <div className="ui-conversation-destinations">
        <button type="button" disabled={Boolean(state.busy)} onClick={copy}><Icon name={state.copied ? 'check' : 'copy'} size={16}/>复制文字</button>
        <button type="button" disabled={Boolean(state.busy)} onClick={image}><Icon name="down" size={16}/>存为图片</button>
        {state.busy && <StatusDot state="processing"/>}
      </div>
      {state.error && <p role="alert" className="ui-conversation-error">{state.error === 'copy' ? '复制未完成' : '图片未生成'} · <button type="button" onClick={state.error === 'copy' ? copy : image}>重试</button></p>}
      {state.images.length > 0 && <div className="ui-conversation-images" aria-label="图片页">{state.images.map((page, index) => <a key={page.url} href={page.url} download={page.filename} aria-label={`保存图片 ${index + 1} / ${state.images.length}`}><Icon name="down" size={14}/>{index + 1} / {state.images.length}</a>)}</div>}
      <section aria-label="分享预览">
        <h3>问题</h3><div className="ui-conversation-question">{current.question}</div>
        <h3>回答</h3><MarkdownBody>{current.answer}</MarkdownBody>
        {current.citations.length > 0 && <><h3>引用 <span>{current.citations.length}</span></h3><div className="ui-conversation-citations">{current.citations.map((citation, index) => <div key={index}>
          <div className="ui-conversation-citation-label"><span>{citation.title}</span><button type="button" disabled={Boolean(state.busy)} onClick={() => remove(index)} aria-label={`移除引用 ${citation.title}`} title="去掉"><Icon name="close" size={14}/></button></div>
          <blockquote>{citation.quote}</blockquote>
        </div>)}</div></>}
      </section>
    </>}
  </FocusPanel>;
}
