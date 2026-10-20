import { useEffect, useRef, useState } from 'react';
import { createEditableDocumentDelivery, loadEditableDocumentDeliveryArtifact } from '../../features/rebuild/libraryOverviewApi';
import { productFetch, userStorageKey } from '../api/deviceTransport';
import { useRequestScope } from '../lib/useRequestScope';
import { Icon } from './Icon';
import './DocumentDeliveryAction.css';

const formats = [['markdown', 'Markdown'], ['html', 'HTML'], ['docx', 'DOCX'], ['pptx', 'PPTX']];

export function DocumentDeliveryAction({ projectId, documentId, revision, needsSave, disabled, isSavedRevision }) {
  const identity = () => userStorageKey(JSON.stringify(['document-delivery', projectId, documentId, revision]));
  const scopeKey = identity(), gate = useRequestScope(scopeKey), scope = gate.scope;
  const [open, setOpen] = useState(false), [busy, setBusy] = useState(false), [error, setError] = useState('');
  const entry = useRef(null), controller = useRef(null), saved = useRef(isSavedRevision); saved.current = isSavedRevision;
  function invalidate() { gate.invalidate('delivery'); controller.current?.abort(); }
  useEffect(() => {
    const changed = () => { invalidate(); setBusy(false); setOpen(false); };
    window.addEventListener('chriptmas-user-space', changed);
    window.addEventListener('chriptmas-device-required', changed);
    return () => {
      invalidate();
      window.removeEventListener('chriptmas-user-space', changed);
      window.removeEventListener('chriptmas-device-required', changed);
    };
  }, [scope]);
  useEffect(() => { invalidate(); setBusy(false); setError(''); }, [needsSave, disabled, scope]);
  function close() { setOpen(false); entry.current?.focus(); }
  async function download(format) {
    if (busy || disabled || needsSave || !saved.current(revision)) return;
    const token = gate.issue('delivery', scope), abort = new AbortController(); controller.current = abort;
    const current = () => gate.isCurrent(token) && identity() === scopeKey && saved.current(revision);
    const fetchImpl = (url, options) => productFetch(url, { ...options, signal: abort.signal });
    setBusy(true); setError('');
    try {
      const delivery = await createEditableDocumentDelivery({ projectId, documentId, expectedRevision: revision, formats: [format], fetchImpl });
      if (!current()) return;
      if (delivery.document_id !== documentId || delivery.document_revision !== revision || delivery.status !== 'completed'
        || !delivery.delivery_id || !delivery.artifacts?.some(artifact => artifact.format === format)) throw new Error('document_delivery_invalid');
      const artifact = await loadEditableDocumentDeliveryArtifact({ projectId, deliveryId: delivery.delivery_id, format, fetchImpl });
      if (!current()) return;
      const href = URL.createObjectURL(artifact.blob), link = document.createElement('a');
      link.href = href; link.download = artifact.fileName; link.rel = 'noreferrer';
      document.body.appendChild(link); link.click(); link.remove();
      setTimeout(() => URL.revokeObjectURL(href), 1000);
      close();
    } catch (failure) {
      if (!current() || failure.name === 'AbortError') return;
      setError('导出未完成 · 重试');
    } finally { if (gate.isCurrent(token)) { setBusy(false); controller.current = null; } }
  }
  return <div className="ui-document-delivery" onBlur={event => { if (!event.currentTarget.contains(event.relatedTarget)) setOpen(false); }}
    onKeyDown={event => { if (event.key === 'Escape' && open) { event.preventDefault(); event.stopPropagation(); close(); } }}>
    <button ref={entry} type="button" aria-label="导出文稿" title="导出文稿" aria-expanded={open} onClick={() => setOpen(value => !value)}><Icon name="down" size={16}/></button>
    {open && <div className="ui-document-delivery-options" role="group" aria-label="导出格式" aria-busy={busy}>
      {(needsSave || disabled) && <p role="status">先保存再导出</p>}
      {error && <p role="alert">{error}</p>}
      {formats.map(([format, label]) => <button key={format} type="button" disabled={busy || disabled || needsSave} onClick={() => download(format)}>{label}</button>)}
    </div>}
  </div>;
}
