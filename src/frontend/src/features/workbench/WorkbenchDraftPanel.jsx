import { useEffect, useMemo, useState } from 'react';
import { FocusPanel, SourceDraftView, StatusDot } from '../../shared/ui';
import { DocumentMarkdownEditor } from '../../shared/ui/DocumentMarkdownEditor';
import { useRequestScope } from '../../shared/lib/useRequestScope';
import { libraryApi } from '../library/libraryApi';
import { workbenchApi } from './workbenchApi';
import { ProductFileLink } from '../../shared/ui/ProductFileLink';

export function WorkbenchDraftPanel({ projectId, documentId, itemId, onClose }) {
  const gate = useRequestScope(`${projectId}:${documentId}:${itemId || ''}`), scope = gate.scope;
  const [imageRead, setImageRead] = useState(null);
  const images = gate.isScopeCurrent(imageRead?.scope) ? imageRead.images : [];
  const [detail, setDetail] = useState(null), [source, setSource] = useState(null);
  const [sourceId, setSourceId] = useState('');
  const [loading, setLoading] = useState(true), [error, setError] = useState('');
  useEffect(() => {
    const request = gate.issue('draft', scope);
    setImageRead(null);
    async function load() {
      try {
        const data = await libraryApi.drill(projectId, 'note', documentId);
        if (!gate.isCurrent(request)) return;
        setDetail(data);
        libraryApi.opened(projectId, 'document', documentId);
        const id = data.source?.id || (data.sources?.length === 1 ? data.sources[0].id : '');
        setSourceId(id);
        if (itemId && data.sources.some(source => source.id === itemId && source.kind === 'image')) {
          const imageRequest = gate.issue('images', scope);
          workbenchApi.images(projectId, itemId).then(value => {
            if (gate.isCurrent(imageRequest)) setImageRead({ scope, images: value.images });
          }).catch(() => {});
        }
      } catch { if (gate.isCurrent(request)) setError('读取未完成'); }
      finally { if (gate.isCurrent(request)) setLoading(false); }
    }
    load();
    return () => { gate.invalidate('draft'); gate.invalidate('images'); };
  }, [projectId, documentId, itemId]);
  useEffect(() => {
    const request = gate.issue('source', scope);
    setSource(null);
    if (!sourceId) return;
    libraryApi.sourceText(projectId, sourceId).then(value => {
      if (gate.isCurrent(request)) setSource(value);
    }).catch(() => { if (gate.isCurrent(request)) setError('原件未打开'); });
    return () => gate.invalidate('source');
  }, [projectId, sourceId]);
  // 轮询重渲染保持定位，正文或原件变化仍重置原句高亮。
  const draft = useMemo(() => ({ ...detail?.note, summary: detail?.summary?.text }), [detail?.note, detail?.summary?.text, sourceId]);
  const sources = detail?.sources || [], original = source || sources.find(item => item.id === sourceId);
  const href = source?.url || source?.download_url;
  return <FocusPanel title="整理稿" className="workbench-draft-panel" onClose={onClose}>
    {loading ? <StatusDot state="processing"/> : !detail ? <p role="alert">{error}</p> : <>
      {error && <p role="alert">{error}</p>}
      <SourceDraftView source={source?.text || ''} draft={draft} highlightFacts
        sourceTitle={original?.title || '原文'}
        sourceControls={<>{sources.length > 1 && <select aria-label="原件" value={sourceId} onChange={event => { setError(''); setSourceId(event.target.value); }}><option value="">选择原件</option>{sources.map(item => <option key={item.id} value={item.id}>{item.title}</option>)}</select>}{images.length ? images.map(image => <ProductFileLink key={image.ordinal} href={image.url} name={image.name} scopeKey={`${scope.key}:${image.ordinal}`}>{image.name}</ProductFileLink>) : href && <ProductFileLink href={href} name={original?.title} scopeKey={`${scope.key}:${sourceId}`}>打开原件</ProductFileLink>}</>}
        renderedEditor={({ locateFact }) => <DocumentMarkdownEditor projectId={projectId} documentId={documentId} document={detail.note} label="整理稿正文"
          evidence={detail.note.facts} onEvidence={locateFact} readDocument={async () => (await libraryApi.drill(projectId, 'note', documentId)).note}
          onSaved={note => { if (gate.isScopeCurrent(scope)) setDetail(current => ({ ...current, note: { ...current.note, ...note } })); }}/>}/>
    </>}
  </FocusPanel>;
}
