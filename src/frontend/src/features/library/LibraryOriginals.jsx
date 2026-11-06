import { useEffect, useRef, useState } from 'react';
import { useRequestScope } from '../../shared/lib/useRequestScope';
import { ProductFileLink } from '../../shared/ui/ProductFileLink';
import { AudioEvidenceClip } from '../../shared/ui/AudioEvidenceClip';
import { SourceHighlight } from '../../shared/ui/sourceEvidence';
import { workbenchApi } from '../workbench/workbenchApi';
import { libraryApi } from './libraryApi';

export function OriginalFiles({ projectId, original, scopeKey, images = workbenchApi.images, namespace }) {
  const gate = useRequestScope(scopeKey), scope = gate.scope;
  const [reading, setReading] = useState(null), [retry, setRetry] = useState(0);
  const uploadedImage = original.kind === 'image' && Boolean(original.download_url);
  useEffect(() => {
    if (!uploadedImage) return;
    const token = gate.issue('images', scope), controller = new AbortController();
    setReading({ scope, loading: true });
    images(projectId, original.id, { signal: controller.signal })
      .then(value => {
        if (namespace !== undefined && value.namespace !== namespace) throw new Error('original_namespace_changed');
        if (gate.isCurrent(token)) setReading({ scope, images: value.images });
      })
      .catch(() => { if (gate.isCurrent(token)) setReading({ scope, error: true }); });
    return () => { controller.abort(); gate.invalidate('images'); };
  }, [projectId, original.id, uploadedImage, scope, retry, images, namespace]);
  if (!uploadedImage) return (original.url || original.download_url) &&
    <ProductFileLink href={original.url || original.download_url} name={original.title} scopeKey={scopeKey}>打开原件</ProductFileLink>;
  const current = gate.isScopeCurrent(reading?.scope) ? reading : null;
  return <div className="library-original">
    {!current || current.loading ? <span role="status">读取中</span> : current.error ?
      <div className="library-error" role="alert"><span>读取未完成</span><button type="button" onClick={event => {
        event.currentTarget.closest('.ui-focus-panel')?.querySelector('.ui-focus-close')?.focus();
        setRetry(value => value + 1);
      }}>重试</button></div> :
      <ol aria-label="图片原件">{current.images.map(image => <li key={image.ordinal} value={image.ordinal}>
        <ProductFileLink href={image.url} name={image.name} scopeKey={`${scopeKey}:${image.ordinal}`}>{image.name}</ProductFileLink>
      </li>)}</ol>}
  </div>;
}

export function OriginalText({ projectId, original, scopeKey, api = libraryApi, audioClips = true, namespace }) {
  const window = original.window;
  const [expanded, setExpanded] = useState(!window), [reading, setReading] = useState(null), [retry, setRetry] = useState(0);
  const key = JSON.stringify([scopeKey, projectId, original.id, namespace, retry]);
  const current = reading?.key === key ? reading : null;
  const excerpt = useRef(null), passage = useRef(null), mark = useRef(null), requestedLocation = useRef(false);
  const full = current?.data;
  let start = -1;
  if (full && window?.quote) {
    const context = window.pre + window.quote + window.post;
    const position = full.text.indexOf(context);
    if (position >= 0 && full.text.indexOf(context, position + 1) < 0) start = position + window.pre.length;
    else {
      const single = full.text.indexOf(window.quote);
      if (single >= 0 && full.text.indexOf(window.quote, single + 1) < 0) start = single;
    }
  }
  useEffect(() => {
    if (!expanded || current?.data) return;
    const controller = new AbortController();
    api.sourceText(projectId, original.id, { signal: controller.signal })
      .then(result => {
        if (typeof result.text !== 'string' || (namespace !== undefined && result.namespace !== namespace)) throw new Error('original_namespace_changed');
        if (!controller.signal.aborted) setReading({ key, data: result });
      })
      .catch(() => { if (!controller.signal.aborted) setReading({ key, error: true }); });
    return () => controller.abort();
  }, [key, expanded, api]);
  function locate() {
    passage.current?.focus({ preventScroll: true });
    (mark.current || passage.current)?.scrollIntoView?.({ block: 'center' });
  }
  useEffect(() => {
    if (expanded && full && requestedLocation.current) {
      requestedLocation.current = false;
      locate();
    }
  }, [expanded, full]);
  function expand() { requestedLocation.current = true; setExpanded(true); }
  function returnToExcerpt() {
    requestedLocation.current = false;
    setExpanded(false);
    excerpt.current?.focus({ preventScroll: true });
    excerpt.current?.scrollIntoView?.({ block: 'nearest' });
  }
  return <>
    {window && <><p ref={excerpt} tabIndex={-1} className="library-original" aria-label="原句">{window.pre}<mark>{window.quote}</mark>{window.post}</p>
      {audioClips && <AudioEvidenceClip projectId={projectId} sourceId={original.id} seconds={window.seconds} scopeKey={scopeKey}/>}
      <div className="library-actions"><button type="button" aria-expanded={expanded} onClick={expanded ? returnToExcerpt : expand}>{expanded ? '收起全文' : '展开全文'}</button>
        {expanded && full && start >= 0 && <button type="button" onClick={locate}>定位全文</button>}</div></>}
    {expanded && !current && <p className="library-meta" role="status">读取全文中</p>}
    {expanded && current?.error && <div className="library-error" role="alert"><span>全文未读取</span><button type="button" onClick={() => setRetry(value => value + 1)}>重试</button></div>}
    {expanded && full && <><h3 ref={passage} tabIndex={-1}>全文</h3>
      {window && start < 0 && <p className="library-meta" role="status">全文未能定位原句</p>}
      <p className="library-original">{start >= 0 ? <SourceHighlight source={full.text} quote={window.quote} start={start} markRef={mark}/> : full.text || '没有可读正文'}</p>
      {window && <div className="library-actions"><button type="button" onClick={returnToExcerpt}>回到原句</button></div>}</>}
  </>;
}
