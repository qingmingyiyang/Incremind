import { useEffect, useState } from 'react';
import { FocusPanel, StatusDot } from '../../shared/ui';
import { ProductFileLink } from '../../shared/ui/ProductFileLink';
import { useRequestScope } from '../../shared/lib/useRequestScope';
import { workspaceApi } from '../../shared/api/workspaceApi';
import { workbenchApi } from './workbenchApi';

export function WorkbenchOriginalPanel({ projectId, itemId, onClose }) {
  const gate = useRequestScope(`${projectId}:${itemId}`), scope = gate.scope;
  const [reading, setReading] = useState(null), [retry, setRetry] = useState(0);
  useEffect(() => {
    const token = gate.issue('source', scope), controller = new AbortController();
    setReading({ scope, loading: true });
    workspaceApi.source(projectId, itemId, { signal: controller.signal }).then(async source => {
      if (!gate.isCurrent(token)) return;
      const images = source.input_kind === 'image' ? (await workbenchApi.images(projectId, itemId)).images : null;
      if (gate.isCurrent(token)) setReading({ scope, source, images });
    }).catch(() => { if (gate.isCurrent(token)) setReading({ scope, error: true }); });
    return () => { controller.abort(); gate.invalidate('source'); };
  }, [projectId, itemId, retry, scope]);
  const current = reading?.scope === scope ? reading : null;
  const loading = !current || current.loading, source = current?.source;
  const href = source?.original_download_url || source?.original_url;
  return <FocusPanel title="原件" className="workbench-draft-panel" onClose={onClose} focusScope={`${scope.key}:${Boolean(loading)}:${Boolean(current?.error)}:${retry}`}>
    {loading ? <StatusDot state="processing"/> : current.error ? <p role="alert">原件未打开 <button type="button" onClick={() => setRetry(value => value + 1)}>重试</button></p> : <section aria-label="原文" style={{ overflowWrap: 'anywhere' }}>
      <div className="ui-source-heading"><h3>{source.title}</h3>{current.images ? current.images.map(image => <ProductFileLink key={image.ordinal} href={image.url} name={image.name} scopeKey={`${scope.key}:${image.ordinal}`}>{image.name}</ProductFileLink>) : href && <ProductFileLink href={href} name={source.title} scopeKey={scope.key}>{source.original_download_url ? '下载原件' : '打开原件'}</ProductFileLink>}</div>
      <div className="ui-source-text" style={{ overflowWrap: 'anywhere' }}>{source.text}</div>
    </section>}
  </FocusPanel>;
}
