import { useEffect, useState } from 'react';
import { useRequestScope } from '../../shared/lib/useRequestScope';
import { ProductFileLink } from '../../shared/ui/ProductFileLink';
import { AudioEvidenceClip } from '../../shared/ui/AudioEvidenceClip';
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
  const [expanded, setExpanded] = useState(!original.window), [full, setFull] = useState(null), [error, setError] = useState(''), [retry, setRetry] = useState(0);
  useEffect(() => {
    if (!expanded) return;
    const controller = new AbortController();
    setFull(null); setError('');
    api.sourceText(projectId, original.id, { signal: controller.signal })
      .then(result => {
        if (namespace !== undefined && result.namespace !== namespace) throw new Error('original_namespace_changed');
        if (!controller.signal.aborted) setFull(result);
      })
      .catch(() => { if (!controller.signal.aborted) setError('读取未完成 · 重试'); });
    return () => controller.abort();
  }, [projectId, original.id, expanded, retry, api, namespace]);
  return <>
    {expanded ? full && <p className="library-original">{full.text}</p> : <p className="library-original">{original.window.pre}{audioClips && <AudioEvidenceClip projectId={projectId} sourceId={original.id} seconds={original.window.seconds} scopeKey={scopeKey}/>}<mark>{original.window.quote}</mark>{original.window.post}</p>}
    {!expanded && <div className="library-actions"><button type="button" onClick={() => setExpanded(true)}>展开全文</button></div>}
    {error && <div className="library-error" role="alert"><span>{error}</span><button type="button" onClick={() => setRetry(value => value + 1)}>重试</button></div>}
  </>;
}
