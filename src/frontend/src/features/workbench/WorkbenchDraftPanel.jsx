import { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import { FocusPanel, Row, SourceDraftView, StatusDot } from '../../shared/ui';
import { DocumentMarkdownEditor } from '../../shared/ui/DocumentMarkdownEditor';
import { useRequestScope } from '../../shared/lib/useRequestScope';
import { libraryApi } from '../library/libraryApi';
import { workbenchApi } from './workbenchApi';
import { ProductFileLink } from '../../shared/ui/ProductFileLink';
import { deviceIdentity, isDeviceIdentity, userStorageKey } from '../../shared/api/deviceTransport';

export function WorkbenchDraftPanel(props) {
  const [epoch, setEpoch] = useState(0);
  useEffect(() => {
    const changed = () => setEpoch(value => value + 1);
    window.addEventListener('chriptmas-user-space', changed);
    window.addEventListener('chriptmas-device-required', changed);
    return () => { window.removeEventListener('chriptmas-user-space', changed); window.removeEventListener('chriptmas-device-required', changed); };
  }, []);
  return <DraftSession key={`${userStorageKey(props.projectId)}:${props.documentId}:${props.itemId || ''}:${epoch}`} {...props}/>;
}

function DraftSession({ projectId, documentId, itemId, navigation, onClose }) {
  const gate = useRequestScope(`${projectId}:${documentId}:${itemId || ''}`), scope = gate.scope;
  const actor = deviceIdentity(), userSpace = userStorageKey(projectId);
  const current = request => gate.isCurrent(request) && isDeviceIdentity(actor) && userStorageKey(projectId) === userSpace;
  const [imageRead, setImageRead] = useState(null);
  const images = gate.isScopeCurrent(imageRead?.scope) ? imageRead.images : [];
  const [detail, setDetail] = useState(null), [source, setSource] = useState(null);
  const [sourceId, setSourceId] = useState('');
  const [loading, setLoading] = useState(true), [error, setError] = useState('');
  const [refresh, setRefresh] = useState(0), [sourceRefresh, setSourceRefresh] = useState(0), [imageRefresh, setImageRefresh] = useState(0);
  const [sourceError, setSourceError] = useState(''), [imageError, setImageError] = useState('');
  const [verifying, setVerifying] = useState(false), [verifyError, setVerifyError] = useState(''), [editorState, setEditorState] = useState(null);
  const [editorGeneration, setEditorGeneration] = useState(0);
  const pending = useRef(false), editing = useRef(null);
  editing.current = editorState;
  const editorChanged = useCallback(value => setEditorState(value), []);
  useEffect(() => {
    const request = gate.issue('draft', scope);
    setLoading(true); setError('');
    async function load() {
      try {
        const data = await libraryApi.drill(projectId, 'note', documentId);
        if (!current(request)) return;
        const editor = editing.current;
        if (editor && !editor.dirty && !editor.saving && !editor.conflict && editor.revision !== data.note?.revision) {
          setEditorState(null); setEditorGeneration(value => value + 1);
        }
        setDetail(data);
        libraryApi.opened(projectId, 'document', documentId);
        const id = data.source?.id || (data.sources?.length === 1 ? data.sources[0].id : '');
        setSourceId(id);
      } catch { if (current(request)) setError('读取未完成'); }
      finally { if (current(request)) setLoading(false); }
    }
    load();
    return () => gate.invalidate('draft');
  }, [projectId, documentId, itemId, refresh]);
  const sourceProject = detail?.source_project_id || projectId;
  const imageAvailable = Boolean(itemId && detail?.sources?.some(item => item.id === itemId && item.kind === 'image'));
  useEffect(() => {
    const request = gate.issue('images', scope);
    setImageRead(null); setImageError('');
    if (!imageAvailable) return;
    workbenchApi.images(sourceProject, itemId).then(value => {
      if (current(request)) setImageRead({ scope, images: value.images });
    }).catch(() => { if (current(request)) setImageError('图片未打开'); });
    return () => gate.invalidate('images');
  }, [sourceProject, itemId, imageAvailable, imageRefresh]);
  useEffect(() => {
    const request = gate.issue('source', scope);
    setSource(null); setSourceError('');
    if (!sourceId) return;
    libraryApi.sourceText(sourceProject, sourceId).then(value => {
      if (current(request)) setSource(value);
    }).catch(() => { if (current(request)) setSourceError('原件未打开'); });
    return () => gate.invalidate('source');
  }, [sourceProject, sourceId, sourceRefresh]);
  function verificationAllowed() {
    return Boolean(!loading && !verifying && !pending.current && gate.isScopeCurrent(scope)
      && isDeviceIdentity(actor) && userStorageKey(projectId) === userSpace && detail?.note && !detail.readonly
      && (detail.note.document_id || detail.note.id) === documentId && !detail.note.verified
      && Number.isSafeInteger(detail.note.revision) && detail.note.revision > 0 && editorState
      && editorState.revision === detail.note.revision && !editorState.disabled && !editorState.dirty
      && !editorState.saving && !editorState.conflict);
  }
  async function verify() {
    if (!verificationAllowed()) return;
    const request = gate.issue('verify', scope), note = detail.note;
    if (!request) return;
    pending.current = true; setVerifying(true); setVerifyError('');
    try {
      const latest = await libraryApi.drill(projectId, 'note', documentId);
      if (!current(request)) return;
      if (latest.readonly || latest.note?.revision !== note.revision) {
        setVerifyError('内容已变化 · 重新读取'); return;
      }
      const saved = latest.note.verified ? latest.note : await libraryApi.verify(projectId, note);
      if (current(request)) setDetail(value => value?.note?.revision === note.revision
        ? { ...value, note: { ...value.note, verified: saved.verified === true } } : value);
    } catch (failure) { if (current(request)) setVerifyError(failure.status === 409 ? '内容已变化 · 重新读取' : '核对未完成 · 重试'); }
    finally { pending.current = false; if (current(request)) setVerifying(false); }
  }
  // 轮询重渲染保持定位，正文或原件变化仍重置原句高亮。
  const draft = useMemo(() => ({ ...detail?.note, summary: detail?.summary?.text }), [detail?.note, detail?.summary?.text, sourceId]);
  const sources = detail?.sources || [], original = source || sources.find(item => item.id === sourceId);
  const href = source?.url || source?.download_url;
  const canVerify = verificationAllowed();
  return <FocusPanel title="整理稿" className="workbench-draft-panel" onClose={onClose}
    status={detail?.note ? detail.note.verified ? 'done' : 'unverified' : undefined}
    actions={canVerify && <button type="button" disabled={!canVerify} onClick={verify}>核对完成</button>}>
    {navigation}
    {loading && !detail ? <StatusDot state="processing"/> : !detail ? <p role="alert">{error} <button type="button" onClick={() => setRefresh(value => value + 1)}>重试</button></p> : <>
      {error && <p role="alert">{error} <button type="button" disabled={loading || verifying} onClick={() => setRefresh(value => value + 1)}>重试</button></p>}
      {sourceError && <p role="alert">{sourceError} <button type="button" onClick={() => setSourceRefresh(value => value + 1)}>重试</button></p>}
      {imageError && <p role="alert">{imageError} <button type="button" onClick={() => setImageRefresh(value => value + 1)}>重试</button></p>}
      {verifyError && <p role="alert">{verifyError} <button type="button" disabled={!canVerify} onClick={verify}>重试核对</button> <button type="button" disabled={loading || verifying} onClick={() => setRefresh(value => value + 1)}>重新读取</button></p>}
      {!!detail.attachments?.length && <div aria-label="产出附件">{detail.attachments.map(file => <Row key={file.source_id} readOnly title={file.available
        ? <ProductFileLink href={file.download_url} name={file.name} scopeKey={`${scope.key}:attachment:${file.source_id}`}>{file.name}</ProductFileLink>
        : `${file.name} · 附件暂不可用`}/>)}</div>}
      <SourceDraftView source={source?.text || ''} draft={draft} highlightFacts
        sourceTitle={original?.title || '原文'}
        sourceControls={<>{sources.length > 1 && <select aria-label="原件" value={sourceId} onChange={event => setSourceId(event.target.value)}><option value="">选择原件</option>{sources.map(item => <option key={item.id} value={item.id}>{item.title}</option>)}</select>}{images.length ? images.map(image => <ProductFileLink key={image.ordinal} href={image.url} name={image.name} scopeKey={`${scope.key}:${image.ordinal}`}>{image.name}</ProductFileLink>) : href && <ProductFileLink href={href} name={original?.title} scopeKey={`${scope.key}:${sourceId}`}>打开原件</ProductFileLink>}</>}
        renderedEditor={({ locateFact }) => <DocumentMarkdownEditor key={editorGeneration} projectId={projectId} documentId={documentId} document={detail.note} label="整理稿正文"
          disabled={detail.readonly || verifying} onEditingStateChange={editorChanged}
          evidence={detail.note.facts} onEvidence={locateFact} readDocument={async () => (await libraryApi.drill(projectId, 'note', documentId)).note}
          onSaved={note => { if (gate.isScopeCurrent(scope)) setDetail(current => ({ ...current, note: { ...current.note, ...note } })); }}/>}/>
    </>}
  </FocusPanel>;
}
