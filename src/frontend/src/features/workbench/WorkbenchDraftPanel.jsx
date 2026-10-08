import { useEffect, useState } from 'react';
import { FocusPanel, RichMarkdownEditor, SourceDraftView, StatusDot } from '../../shared/ui';
import { useRequestScope } from '../../shared/lib/useRequestScope';
import { libraryApi } from '../library/libraryApi';
import { recognitionApi } from '../../shared/api/recognitionApi';
import { workbenchApi } from './workbenchApi';
import { ProductFileLink } from '../../shared/ui/ProductFileLink';

export function WorkbenchDraftPanel({ projectId, documentId, itemId, onClose }) {
  const gate = useRequestScope(`${projectId}:${documentId}:${itemId || ''}`), scope = gate.scope;
  const [images, setImages] = useState([]);
  const [detail, setDetail] = useState(null), [source, setSource] = useState(null);
  const [sourceId, setSourceId] = useState(''), [markdown, setMarkdown] = useState('');
  const [base, setBase] = useState(null), [conflict, setConflict] = useState(null);
  const [loading, setLoading] = useState(true), [saving, setSaving] = useState(false), [error, setError] = useState('');
  useEffect(() => {
    const request = gate.issue('images', scope);
    setImages([]);
    if (itemId) workbenchApi.images(projectId, itemId).then(value => {
      if (gate.isCurrent(request)) setImages(value.images);
    }).catch(() => {});
    return () => gate.invalidate('images');
  }, [projectId, documentId, itemId]);
  useEffect(() => {
    const request = gate.issue('draft', scope);
    async function load() {
      try {
        const data = await libraryApi.drill(projectId, 'note', documentId);
        if (!gate.isCurrent(request)) return;
        setDetail(data); setMarkdown(data.note.markdown); setBase(data.note);
        libraryApi.opened(projectId, 'document', documentId);
        const id = data.source?.id || (data.sources?.length === 1 ? data.sources[0].id : '');
        setSourceId(id);
      } catch { if (gate.isCurrent(request)) setError('读取未完成'); }
      finally { if (gate.isCurrent(request)) setLoading(false); }
    }
    load();
    return () => gate.invalidate('draft');
  }, [projectId, documentId]);
  useEffect(() => {
    const request = gate.issue('source', scope);
    setSource(null);
    if (!sourceId) return;
    libraryApi.sourceText(projectId, sourceId).then(value => {
      if (gate.isCurrent(request)) setSource(value);
    }).catch(() => { if (gate.isCurrent(request)) setError('原件未打开'); });
    return () => gate.invalidate('source');
  }, [projectId, sourceId]);
  async function save(value = markdown) {
    if (saving || !base || conflict) return;
    const request = gate.issue('save', scope), local = value;
    setSaving(true); setError('');
    try {
      const result = await recognitionApi.saveDocument({ documentId, projectId, expectedRevision: base.revision, markdown: local });
      if (!gate.isCurrent(request)) return;
      const { comment_section: _old, ...fields } = base;
      const next = { ...fields, markdown: local, revision: result.revision }; setBase(next);
      libraryApi.drill(projectId, 'note', documentId).then(current => {
        if (gate.isCurrent(request) && current.note?.document_id === documentId && current.note.revision === next.revision && current.note.markdown === local) setBase(value => value?.revision === next.revision && value.markdown === local ? { ...value, comment_section: current.note.comment_section } : value);
      }).catch(() => {}); // Count is optional; the document save has already succeeded.
    } catch (failure) {
      if (!gate.isCurrent(request)) return;
      if (failure.status === 409) {
        try {
          const current = await libraryApi.drill(projectId, 'note', documentId);
          if (gate.isCurrent(request)) setConflict({ base, local, server: current.note });
        } catch { if (gate.isCurrent(request)) setError('读取未完成'); }
      } else setError('保存未完成');
    } finally { if (gate.isCurrent(request)) setSaving(false); }
  }
  function choose(server) {
    setMarkdown(server ? conflict.server.markdown : conflict.local);
    setBase(conflict.server); setConflict(null);
  }
  const sources = detail?.sources || [], original = source || sources.find(item => item.id === sourceId);
  const href = source?.url || source?.download_url;
  return <FocusPanel title="整理稿" className="workbench-draft-panel" onClose={onClose}>
    {loading ? <StatusDot state="processing"/> : !detail ? <p role="alert">{error}</p> : <>
      {error && <p role="alert">{error}</p>}
      <SourceDraftView source={source?.text || ''} draft={{ ...detail.note, summary: detail.summary?.text }} highlightFacts
        sourceTitle={original?.title || '原文'}
        sourceControls={<>{sources.length > 1 && <select aria-label="原件" value={sourceId} onChange={event => { setError(''); setSourceId(event.target.value); }}><option value="">选择原件</option>{sources.map(item => <option key={item.id} value={item.id}>{item.title}</option>)}</select>}{images.length ? images.map(image => <ProductFileLink key={image.ordinal} href={image.url} name={image.name} scopeKey={`${scope.key}:${image.ordinal}`}>{image.name}</ProductFileLink>) : href && <ProductFileLink href={href} name={original?.title} scopeKey={`${scope.key}:${sourceId}`}>打开原件</ProductFileLink>}</>}
        conflict={conflict ? { baseDraft: { markdown: conflict.base.markdown }, editor: { markdown: conflict.local }, server: { markdown: conflict.server.markdown }, fields: [['markdown', '整理稿']], onChoose: choose } : null}
        disabled={saving}
        renderedEditor={({ locateFact }) => <RichMarkdownEditor value={markdown} disabled={saving || Boolean(conflict)} onChange={setMarkdown} onSave={save} evidence={detail.note.facts} onEvidence={locateFact}
          documentId={documentId} documentRevision={base?.revision} documentMarkdown={base?.markdown} commentSection={base?.comment_section}/>}/>
    </>}
  </FocusPanel>;
}
